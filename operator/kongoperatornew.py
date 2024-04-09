# new working code

import kopf
import kubernetes.client
import logging
import json
from kubernetes.client.rest import ApiException
import os

logging_level = os.environ.get('LOGGING',logging.INFO)
print('Logging set to ',logging_level)

kopf_logger = logging.getLogger()
kopf_logger.setLevel(logging.WARNING)

logger = logging.getLogger('APIOperator')
logger.setLevel(int(logging_level))

HTTP_SCHEME = "http://"
HTTP_K8s_LABELS = ['http', 'http2']
HTTP_STANDARD_PORTS = [80, 443]
GROUP = "oda.tmforum.org"
VERSION = "v1beta3"
APIS_PLURAL = "apis"


@kopf.on.create(GROUP, VERSION, APIS_PLURAL, retries=5)
@kopf.on.update(GROUP, VERSION, APIS_PLURAL, retries=5)
def create_or_update_ingress(spec, name, namespace, logger, **kwargs):
    # Initialize Kubernetes client
    api = kubernetes.client.NetworkingV1Api()

    # Construct Ingress name and path from VirtualService details
    #ingress_name = f"kong-to-istio-ingress-{meta['name']}"
    ingress_name = f"kong-ingress-{name}"
    #print(ingress_name)
    try:
        # Attempt to find if the path exists
        path = spec.get('path')
        #print(path)
        if not path:
            logger.warning(f"Path not found   '{name}'. Ingress creation skipped.")
            return

        # Define the Ingress resource , this is working fine
        ingress_manifest = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": ingress_name,
                "namespace": "istio-ingress",  # Target namespace for the Ingress
                "annotations": {
                    "kubernetes.io/ingress.class": "kong",
                    ""
                },
            },
            "spec": {
                "rules": [{
                    "http": {
                        "paths": [{
                            "path": path,
                            "pathType": "Prefix",
                            "backend": {
                                "service": {
                                    "name": "istio-ingress",  #  Istio Ingress service name
                                    "port": {"number": 80},
                                },
                            },
                        }],
                    },
                }],
            },
        }
        # Make it our child: assign the namespace, name, labels, owner references, etc.
        #kopf.adopt(body)      
        #logWrapper(logging.DEBUG, 'create_or_update_ingress', inHandler, 'api/' + inAPIName, componentName, "Virtual Service", body)    


        # Create or update the Ingress resource
        try:
            # Checking if the Ingress already exists
            api.read_namespaced_ingress(name=ingress_name, namespace="istio-ingress")
            # If it does, update it
            response = api.replace_namespaced_ingress(name=ingress_name, namespace="istio-ingress", body=ingress_manifest)
            logger.info(f"Ingress '{ingress_name}' updated in namespace 'istio-ingress'.")
        except ApiException as e:
            if e.status == 404:
                # If it doesn't exist, create it
                response = api.create_namespaced_ingress(namespace="istio-ingress", body=ingress_manifest)
                logger.info(f"Ingress '{ingress_name}' created in namespace 'istio-ingress'.")
            else:
                # Handle other exceptions
                logger.error(f"API exception when accessing Ingress: {e}")
                raise

    except ApiException as e:
        logger.error(f"Failed to create or update Ingress '{ingress_name}': {e}")
        raise kopf.TemporaryError(f"Exception when calling NetworkingV1Api for Ingress '{ingress_name}': {e}", delay=30)
