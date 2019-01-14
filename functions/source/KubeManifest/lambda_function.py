import json
import logging
import threading
from botocore.vendored import requests
import boto3
import subprocess
import shlex
import os
import re


SUCCESS = "SUCCESS"
FAILED = "FAILED"


s3_client = boto3.client('s3')
kms_client = boto3.client('kms')


def send(event, context, response_status, response_data, physical_resource_id, reason=None):
    response_url = event['ResponseURL']
    logging.debug("CFN response URL: " + response_url)
    response_body = dict()
    response_body['Status'] = response_status
    msg = 'See details in CloudWatch Log Stream: ' + context.log_stream_name
    if not reason:
        response_body['Reason'] = msg
    else:
        response_body['Reason'] = str(reason)
    if physical_resource_id:
        response_body['PhysicalResourceId'] = physical_resource_id
    elif 'PhysicalResourceId' in event:
        response_body['PhysicalResourceId'] = event['PhysicalResourceId']
    else:
        response_body['PhysicalResourceId'] = context.log_stream_name
    response_body['StackId'] = event['StackId']
    response_body['RequestId'] = event['RequestId']
    response_body['LogicalResourceId'] = event['LogicalResourceId']
    if response_data and response_data != {} and response_data != [] and isinstance(response_data, dict):
        response_body['Data'] = response_data
    json_response_body = json.dumps(response_body)
    logging.debug("Response body:\n" + json_response_body)
    headers = {
        'content-type': '',
        'content-length': str(len(json_response_body))
    }
    print("Returning response: %s" % json_response_body)
    try:
        response = requests.put(response_url, data=json_response_body, headers=headers)
        logging.info("CloudFormation returned status code: " + response.reason)
    except Exception as e:
        logging.error("send(..) failed executing requests.put(..): " + str(e))
        raise


def timeout(event, context):
    logging.error('Execution is about to time out, sending failure response to CloudFormation')
    send(event, context, FAILED, {}, None)


def run_command(command):
    try:
        print("executing command: %s" % command)
        output = subprocess.check_output(shlex.split(command), stderr=subprocess.STDOUT).decode("utf-8")
        print(output)
    except subprocess.CalledProcessError as exc:
        print("Command failed with exit code %s, stderr: %s" % (exc.returncode, exc.output.decode("utf-8")))
        raise Exception(exc.output.decode("utf-8"))
    return output


def create_kubeconfig(bucket, key, kms_context):
    try:
        os.mkdir("/tmp/.kube/")
    except FileExistsError:
        pass
    print("s3_client.get_object(Bucket='%s', Key='%s')" % (bucket, key))
    try:
        enc_config = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()
    except Exception as e:
        raise Exception("Failed to fetch KubeConfig from S3: %s" % str(e))
    kubeconf = kms_client.decrypt(
        CiphertextBlob=enc_config,
        EncryptionContext=kms_context
    )['Plaintext'].decode('utf8')
    f = open("/tmp/.kube/config", "w")
    f.write(kubeconf)
    f.close()
    os.environ["KUBECONFIG"] = "/tmp/.kube/config"


def write_manifest(manifest, path):
    f = open(path, "w")
    f.write(json.dumps(manifest))
    f.close()


def generate_name(event, physical_resource_id):
    manifest = event['ResourceProperties']['Manifest']
    stack_name = event['StackId'].split('/')[1]
    if "metadata" in manifest.keys():
        if 'name' not in manifest["metadata"].keys() and 'generateName' not in manifest["metadata"].keys():
            if physical_resource_id:
                manifest["metadata"]["name"] = physical_resource_id.split('/')[-1]
            else:
                manifest["metadata"]["generateName"] = "cfn-%s-" % stack_name.lower()
    return manifest


def build_output(kube_response):
    outp = {}
    for key in ["uid", "selfLink", "resourceVersion", "namespace", "name"]:
        if key in kube_response["metadata"].keys():
            outp[key] = kube_response["metadata"][key]
    return outp


def get_config_details(event):
    s3_uri_parts = event['ResourceProperties']['KubeConfigPath'].split('/')
    if len(s3_uri_parts) < 4 or s3_uri_parts[0:2] != ['s3:', '']:
        raise Exception("Invalid KubeConfigPath, must be in the format s3://bucket-name/path/to/config")
    bucket = s3_uri_parts[2]
    key = "/".join(s3_uri_parts[3:])
    kms_context = {"QSContext": event['ResourceProperties']['KubeConfigKmsContext']}
    return bucket, key, kms_context


def traverse(obj, path=None, callback=None):
    if path is None:
        path = []

    if isinstance(obj, dict):
        value = {k: traverse(v, path + [k], callback)
                 for k, v in obj.items()}
    elif isinstance(obj, list):
        value = [traverse(obj[idx], path + [[idx]], callback)
                 for idx in range(len(obj))]
    else:
        value = obj

    if callback is None:
        return value
    else:
        return callback(path, value)


def traverse_modify(obj, target_path, action):
    target_path = to_path(target_path)

    def transformer(path, value):
        if path == target_path:
            return action(value)
        else:
            return value
    return traverse(obj, callback=transformer)


def traverse_modify_all(obj, action):

    def transformer(path, value):
        return action(value)
    return traverse(obj, callback=transformer)


def to_path(path):
    if isinstance(path, list):
        return path  # already in list format

    def _iter_path(path):
        indexes = [[int(i[1:-1])] for i in re.findall(r'\[[0-9]+\]', path)]
        lists = re.split(r'\[[0-9]+\]', path)
        for parts in range(len(lists)):
            for part in lists[parts].strip('.').split('.'):
                yield part
            if parts < len(indexes):
                yield indexes[parts]
            else:
                yield []
    return list(_iter_path(path))[:-1]


def set_type(input_str):
    if type(input_str) == str:
        if input_str.lower() == 'false':
            return False
        if input_str.lower() == 'true':
            return True
        if input_str.isdigit():
            return int(input_str)
    return input_str


def fix_types(manifest):
    return traverse_modify_all(manifest, set_type)


def lambda_handler(event, context):
    # make sure we send a failure to CloudFormation if the function is going to timeout
    timer = threading.Timer((context.get_remaining_time_in_millis() / 1000.00) - 0.5, timeout, args=[event, context])
    timer.start()
    print('Received event: %s' % json.dumps(event))
    status = SUCCESS
    response_data = {}
    physical_resource_id = None
    error_message = None
    try:
        os.environ["PATH"] = "/var/task/bin:" + os.environ.get("PATH")
        if not event['ResourceProperties']['KubeConfigPath'].startswith("s3://"):
            raise Exception("KubeConfigPath must be a valid s3 URI (eg.: s3://my-bucket/my-key.txt")
        bucket, key, kms_context = get_config_details(event)
        create_kubeconfig(bucket, key, kms_context)
        manifest_file = '/tmp/manifest.json'
        if "PhysicalResourceId" in event.keys():
            physical_resource_id = event["PhysicalResourceId"]
        manifest = fix_types(generate_name(event, physical_resource_id))
        write_manifest(manifest, manifest_file)
        print("Applying manifest: %s" % json.dumps(manifest))
        if event['RequestType'] == 'Create':
            outp = run_command("kubectl create --save-config -o json -f %s" % manifest_file)
            response_data = build_output(json.loads(outp))
            physical_resource_id = response_data["selfLink"]
        if event['RequestType'] == 'Update':
            outp = run_command("kubectl apply -f %s" % manifest_file)
            response_data = build_output(json.loads(outp))
        if event['RequestType'] == 'Delete':
            if not re.search(r'^[0-9]{4}\/[0-9]{2}\/[0-9]{2}\/\[\$LATEST\][a-f0-9]{32}$', physical_resource_id):
                run_command("kubectl delete -f %s" % manifest_file)
            else:
                print("physical_resource_id is not a kubernetes resource, assuming there is nothing to delete")
    except Exception as e:
        logging.error('Exception: %s' % e, exc_info=True)
        status = FAILED
        error_message = str(e)
    finally:
        timer.cancel()
        send(event, context, status, response_data, physical_resource_id, reason=error_message)
