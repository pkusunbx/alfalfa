# !/usr/bin/env python

# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import boto3
import json
import subprocess
import sys
import logging

# Process Message
def process_message(message):
    try:
        message_body = json.loads(message.body)
        message.delete()
        op = message_body.get('op')
        if op == 'InvokeAction':
            action = message_body.get('action')
            if action == 'runSite':
                siteRef = message_body.get('id', 'None')
                startDatetime = message_body.get('startDatetime', 'None')
                endDatetime = message_body.get('endDatetime', 'None')
                realtime = message_body.get('realtime', 'None')
                timescale = str(message_body.get('timescale', 'None'))

                logger.info('Start simulation for site_ref: %s' % siteRef)
                subprocess.call(['python', 'runSite.py', siteRef, realtime, timescale, startDatetime, endDatetime])
            elif action == 'addSite':
                osm_name = message_body.get('osm_name')
                upload_id = message_body.get('upload_id')
                logger.info('Add site for osm_name: %s, and upload_id: %s' % (osm_name, upload_id))
                subprocess.call(['python', 'addSite.py', osm_name, upload_id])
    except Exception as e:
        print('Exception while processing message: %s' % e, file=sys.stderr)

# ======================================================= MAIN ========================================================
if __name__ == '__main__':
    try:
        sqs = boto3.resource('sqs', region_name='us-east-1', endpoint_url=os.environ['JOB_QUEUE_URL'])
        queue = sqs.Queue(url=os.environ['JOB_QUEUE_URL'])
        s3 = boto3.resource('s3', region_name='us-east-1')

        logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
        logger = logging.getLogger('worker')
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        fh = logging.FileHandler('worker.log')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)
    except:
        print('Exception while starting up worker', file=sys.stderr)
        sys.exit(1)

    while True:
        # WaitTimeSeconds triggers long polling that will wait for events to enter queue
        # Receive Message
        messages = queue.receive_messages(MaxNumberOfMessages=1, WaitTimeSeconds=20)
        if len(messages) > 0:
            msg = messages[0]
            logger.info('Message Received with payload: %s' % msg.body)
            # Process Message
            process_message(msg)
        else:
            if "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" in os.environ:
                ecsclient = boto3.client('ecs', region_name='us-east-1')
                response = ecsclient.describe_services(cluster='worker_ecs_cluster',services=['worker-service'])['services'][0]
                desiredCount = response['desiredCount']
                runningCount = response['runningCount']
                pendingCount = response['pendingCount']
                minimumCount = 1

                if ((runningCount > minimumCount) & (desiredCount > minimumCount)):
                    ecsclient.update_service(cluster='worker_ecs_cluster',
                        service='worker-service',
                        desiredCount=(desiredCount - 1))
                    sys.exit(0)
