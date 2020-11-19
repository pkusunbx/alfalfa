from __future__ import print_function

import glob
import os
import shutil
import sys
import tarfile
import zipfile
from subprocess import call
import json

# Local
from alfalfa_worker.add_site.add_site_logger import AddSiteLogger
from alfalfa_worker.lib import precheck_argus, make_ids_unique, replace_site_id
from alfalfa_worker.lib.alfalfa_connections import AlfalfaConnections


class AddSite:
    """A wrapper class around adding sites"""

    def __init__(self, fn, up_id, f_dir):
        """
        Initialize.
        :param fn: name of file to submit
        :param up_id: upload_id as first created by Upload.js when sending file to file s3 bucket (addSiteResolver)
        :param f_dir: directory to upload to on s3 bucket after parsing: parsed/{site_id}/.  Also used locally during this process.
        """
        self.add_site_logger = AddSiteLogger()
        self.add_site_logger.logger.info("AddSite called with args: {} {} {}".format(fn, up_id, f_dir))
        self.file_name = fn
        self.upload_id = up_id
        self.bucket_parsed_site_id_dir = f_dir
        _, self.file_ext = os.path.splitext(self.file_name)
        self.key = "uploads/%s/%s" % (self.upload_id, self.file_name)

        # Define OSM and OSW specific attributes
        self.seed_osm_path = os.path.join(self.bucket_parsed_site_id_dir, 'seed.osm')
        self.workflow_osw_path = os.path.join(self.bucket_parsed_site_id_dir, 'workflow/workflow.osw')
        self.epw_path = os.path.join(self.bucket_parsed_site_id_dir, 'workflow/files/weather.epw')  # mainly only used for add_osw
        self.report_haystack_json = os.path.join(self.bucket_parsed_site_id_dir, 'workflow/reports/haystack_report_haystack.json')
        self.report_mapping_json = os.path.join(self.bucket_parsed_site_id_dir, 'workflow/reports/haystack_report_mapping.json')

        # Define FMU specific attributes
        self.fmu_path = os.path.join(self.bucket_parsed_site_id_dir, 'model.fmu')
        self.fmu_json = os.path.join(self.bucket_parsed_site_id_dir, 'tags.json')

        # Create connections
        self.ac = AlfalfaConnections()

        # Needs to be set after files are uploaded / parsed.
        self.site_ref = None

    def main(self):
        """
        Main entry point after init.  Adds site based on file ext.  Worfklow is generally as follows:
        1. Download file from s3 bucket
        2. Ingest model file and add tags
        3. Send data to mongo and s3 bucket
        4. Remove files generated during this process
        :return:
        """
        if self.file_ext == '.osm':
            self.add_osm()
        elif self.file_ext == '.zip':
            self.add_osw()
        elif self.file_ext == '.fmu':
            self.add_fmu()
        else:
            self.add_site_logger.logger.error("Unsupported file extension: {}".format(self.file_ext))
            os.exit(1)

    def extract_workflow_tar(self):
        """
        Extract workflow tarball into this directory
        :return:
        """
        tar = tarfile.open("workflow.tar.gz")
        tar.extractall(self.bucket_parsed_site_id_dir)
        tar.close()

    def get_site_ref(self, haystack_json):
        """
        Find the site given the haystack JSON file.  Remove 'r:' from string.
        :param haystack_json: json serialized Haystack document
        :return: site_ref: id of site
        """
        site_ref = ''
        with open(haystack_json) as json_file:
            data = json.load(json_file)
            for entity in data:
                if 'site' in entity:
                    if entity['site'] == 'm:':
                        site_ref = entity['id'].replace('r:', '')
                        break
        return site_ref

    def os_files_final_touches_and_upload(self):
        """
        Make unique ids and replace site_id.  Upload to mongo and filestore.
        :return:
        """
        # This is neccessary to avoid avoid duplicates in the case that the same osm / osw is uploaded multiple times
        # In alfalfa multiple uploades are treated as unique "sites"
        make_ids_unique(self.upload_id, self.report_haystack_json, self.report_mapping_json)
        replace_site_id(self.upload_id, self.report_haystack_json, self.report_mapping_json)

        # Upload to mongo & filestore, clean local directory
        self.add_to_mongo_and_filestore()

    def add_to_mongo(self, site_ref, points_json):
        """
        Attempt upload to mongo and filestore.  Function exits if not uploaded correctly
        based on expected return values from methods in AlfalfaConnections.
        :return:
        """
        if self.file_ext != '.fmu':
            f = self.report_haystack_json
            self.site_ref = self.get_site_ref(self.report_haystack_json)
        else:
            f = self.fmu_json
            self.site_ref = self.get_site_ref(self.fmu_json)

        # Check mongo upload works correctly
        mongo_response = self.ac.add_site_to_mongo(points_json, site_ref)
        if len(mongo_response.inserted_ids) > 0:
            self.add_site_logger.logger.info('added {} ids to MongoDB under site_ref: {}'.format(len(mongo_response.inserted_ids), self.site_ref))
        else:
            self.add_site_logger.logger.warning('site not added to MongoDB under site_ref: {}'.format(self.site_ref))
            sys.exit(1)

        # Check filestore upload works correctly
        filestore_response, output = self.ac.add_site_to_filestore(self.bucket_parsed_site_id_dir, self.site_ref)

        # Clean local directory
        shutil.rmtree(self.bucket_parsed_site_id_dir)
        if filestore_response:
            self.add_site_logger.logger.info(
                'added to filestore: {}'.format(output))
        else:
            self.add_site_logger.logger.warning('site not added to filestore - exception: {}'.format(output))
            sys.exit(1)

    def add_osm(self):
        """
        Workflow for osm.
        :return:
        """
        self.add_site_logger.logger.info("add_osm for {}".format(self.key))
        self.ac.s3_bucket.download_file(self.key, self.seed_osm_path)

        # Extract workflow tarball into this directory
        self.extract_workflow_tar()

        # Run OS Workflow on uploaded file to apply afalfa necessary measures
        call(['openstudio', 'run', '-m', '-w', self.workflow_osw_path])

        self.os_files_final_touches_and_upload()

    def add_osw(self):
        """
        Workflow for osw.  
        This function must merge the "built in" haystack workflow measures with
        the user measure, and then run the resulting combined workflow
        :return:
        """
        self.add_site_logger.logger.info("add_osw for {}".format(self.key))

        # download and extract the payload
        payload_dir = os.path.join(self.bucket_parsed_site_id_dir, 'payload/')
        os.mkdir(payload_dir) 
        payload_file_path = os.path.join(payload_dir, 'in.zip')
        self.ac.s3_bucket.download_file(self.key, payload_file_path)
        zzip = zipfile.ZipFile(payload_file_path)
        workflow_dir = os.path.join(self.bucket_parsed_site_id_dir, 'workflow/')
        zzip.extractall(workflow_dir)

        osws = glob.glob(("%s/**/*.osw" % workflow_dir), recursive=True)
        if osws:
            # there is only support for one osw at this time
            submitted_osw_path = osws[0]
            submitted_workflow_path = os.path.dirname(submitted_osw_path)
        else:
            sys.exit(1)

        # locate the "default" workflow
        default_workflow_path = '/alfalfa/alfalfa_worker/workflow/'
        default_measure_paths = [os.path.join(default_workflow_path, 'measures/')]
        default_osw_path = os.path.join(default_workflow_path, 'workflow.osw')

        # Merge the default workflow measures into the user submitted workflow
        with open(default_osw_path, 'r') as osw:
            data=osw.read()
        default_osw = json.loads(data)

        with open(submitted_osw_path, 'r') as osw:
            data=osw.read()
        submitted_osw = json.loads(data)

        default_steps = default_osw['steps']
        submitted_steps = submitted_osw['steps']
        final_steps = submitted_steps
        final_steps[1:1] = default_steps
        submitted_osw['steps'] = final_steps

        submitted_measure_paths = submitted_osw['measure_paths']
        submitted_osw['measure_paths'] = default_measure_paths + submitted_measure_paths

        with open(submitted_osw_path, 'w') as fp:
            json.dump(submitted_osw, fp)

        # run workflow
        call(['openstudio', 'run', '-m', '-w', submitted_osw_path])

        # load mapping and points json files generated by previous workflow
        points_json_path = submitted_workflow_path + '/reports/haystack_report_haystack.json'
        with open(points_json_path, 'r') as f:
            data=f.read()
        points_json = json.loads(data)

        mapping_json_path = submitted_workflow_path + '/reports/haystack_report_mapping.json'
        with open(mapping_json_path, 'r') as f:
            data=f.read()
        mapping_json = json.loads(data)

        # fixup tags
        points_json, mapping_json = make_ids_unique(points_json, mapping_json)
        points_json = replace_site_id(self.upload_id, points_json)

        # add points to database
        mongo_response = self.ac.add_site_to_mongo(points_json, self.upload_id)

        # create a "simulation" directory that has everything required for simulation
        simulation_dir = os.path.join(self.bucket_parsed_site_id_dir, 'simulation/')
        os.mkdir(simulation_dir) 
        shutil.copy(submitted_workflow_path + '/run/in.idf', simulation_dir + '/sim.idf')
        shutil.copy(submitted_workflow_path + '/reports/haystack_report_mapping.json', simulation_dir)
        shutil.copy(submitted_workflow_path + '/reports/export_bcvtb_report_variables.cfg', simulation_dir + '/variables.cfg')
        # hack. need to find a more general approach to preserve osw resources that might be needed at simulation time
        for file in glob.glob(submitted_workflow_path + '/python/*'):
            shutil.copy(file, simulation_dir)

        # find weather file (if) defined by osw and copy into simulation directory
        epw_name = submitted_osw['weather_file']
        if epw_name:
            epw_file_path = self.find_file(epw_name, submitted_workflow_path) 
            shutil.copyfile(epw_file_path, simulation_dir + '/sim.epw')

        # push entire directory to file storage
        filestore_response, output = self.ac.add_site_to_filestore(self.bucket_parsed_site_id_dir, self.upload_id)

        # remove directory
        shutil.rmtree(self.bucket_parsed_site_id_dir)

    def find_file(self, name, path):
        for root, dirs, files in os.walk(path):
            if name in files:
                return os.path.join(root, name)

    def add_fmu(self):
        """
        Workflow for fmu.  External call to python2 must be made since currently we are using an
        old version of the Modelica Buildings Library and JModelica.
        :return:
        """
        self.add_site_logger.logger.info("add_fmu for {}".format(self.key))

        self.ac.s3_bucket.download_file(self.key, self.fmu_path)

        # External call to python2 to create FMU tags
        call(['python', 'lib/fmu_create_tags.py', self.fmu_path, self.file_name, self.fmu_json])

        self.add_to_mongo_and_filestore()


if __name__ == "__main__":
    args = sys.argv
    file_name, upload_id, directory = precheck_argus(args)
    adder = AddSite(file_name, upload_id, directory)
    adder.main()
