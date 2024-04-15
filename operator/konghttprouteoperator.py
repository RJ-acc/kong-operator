import kopf
import kubernetes.client
import logging
import json
from kubernetes.client.rest import ApiException
from kubernetes.client.api import CustomObjectsApi
import os
import yaml
import requests


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
APIS_PLURAL = "exposedapis"
httproute_uid = None

group = "gateway.networking.k8s.io"  # API group for Gateway API
version = "v1"  # Currently tested on v1 ,need to check with v1alpha1, v1alpha2, v1beta1, etc.
plural = "httproutes"  # The plural name of the kong route CRD - HTTPRoute resource 

 

@kopf.on.create(GROUP, VERSION, APIS_PLURAL, retries=5)
@kopf.on.update(GROUP, VERSION, APIS_PLURAL, retries=5)
def manage_api_lifecycle(spec, name, namespace, status, meta, logger, **kwargs):
    httproute_created = create_or_update_ingress(spec, name, namespace, meta, logger)
    if not httproute_created:
        logger.info("HTTPRoute creation/update failed. Skipping plugin/policy management.")
        return

    plugin_names = []

    # Check if Rate Limit is enabled 
    if spec.get('rateLimit', {}).get('enabled', False):
        ratelimit_plugin = manage_ratelimit(spec, name, namespace, meta, logger)
        if ratelimit_plugin:
            plugin_names.append(ratelimit_plugin)

    # Check if API Key Verification is enabled  
    if spec.get('apiKeyVerification', {}).get('enabled', False):
        apiauth_plugin = manage_apiauthentication(spec, name, namespace, meta, logger)
        if apiauth_plugin:
            plugin_names.append(apiauth_plugin)

    # Check if CORS is enabled 
    if spec.get('CORS', {}).get('enabled', False):
        cors_plugin = manage_cors(spec, name, namespace, meta, logger)
        if cors_plugin:
            plugin_names.append(cors_plugin)
    
    # Manage plugins from URL if provided  in template and collect their names generated after applying in cluster
    url_plugin_names = manage_plugins_from_url(spec, name, namespace, meta, logger)
    plugin_names.extend(url_plugin_names)

    # Update the HTTPRoute with annotations if any plugins were created or updated.
    if plugin_names:
        annotations = {'konghq.com/plugins': ','.join(plugin_names)}
        updated = update_httproute_annotations(name, namespace, annotations, logger)
        if updated:
            logger.info(f"HTTPRoute '{name}' updated with plugins: {plugin_names}")
        else:
            logger.error("Failed to update HTTPRoute with annotations.")


def create_or_update_ingress(spec, name, namespace, meta, logger, **kwargs):
    global httproute_uid
    # Check if 'implementation' is 'ready' this will be enabled once APIS_PLURAL = "exposedapis" will be used in components operator 
    """
    if not status.get('implementation', {}).get('ready', False):
        logger.info(f"Implementation not ready for '{name}'. Ingress creation or update skipped.")
        return
    """
    # Initialize Kubernetes  custom obecclient
    api_instance = kubernetes.client.CustomObjectsApi()
    

    # Construct Ingress name and path from VirtualService details
    #ingress_name = f"kong-to-istio-ingress-{meta['name']}"
    ingress_name = f"kong-api-route-{name}"
    namespace = "istio-ingress" 
    service_name = "istio-ingress"
    service_namespace = "istio-ingress"
    strip_path = "false"
    kong_gateway_namespace = "istio-ingress"


    # Prepare ownerReference ,this will add owner refernece from exposedapis to http  route
    owner_references = [{
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ExposedAPIs",
        "name": name,
        "uid": meta.get('uid'),
        "controller": True,
        "blockOwnerDeletion": True, #confirm this and change to True/False
    }]

    # Generate all annotations based on the CRD spec, passing the namespace
    #all_annotations = generate_annotations(spec, namespace)

    try:
        # Attempt to find if the path exists
        path = spec.get('path')
        print(path)
        if not path:
            logger.warning(f"Path not found   '{name}'. Ingress creation skipped.")
            return

        httproute_manifest = {
            "apiVersion": f"{group}/{version}",
            "kind": "HTTPRoute",
            "metadata": {
                "name": ingress_name,
                "namespace": namespace,
                "annotations": {
                    "konghq.com/strip-path": strip_path,
                    # Merge all policies with existing annotations
                    #**all_annotations,
                },
                "ownerReferences": owner_references
            },
            "spec": {
                "parentRefs": [
                    {
                        "name": "kong",
                        "namespace": kong_gateway_namespace,
                    },
                ],
                "rules": [
                    {
                        "matches": [    
                            {
                                "path": {
                                    "type": "PathPrefix",
                                    "value": path,
                                },
                            },
                        ],
                        "backendRefs": [
                            {
                                "name": service_name,
                                "kind": "Service",
                                "port": 80,
                            },
                        ],
                    },
                ],
            },
        }

        try:
            # Check if the HTTPRoute already exists
            existing_route = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=ingress_name)
            # If it does, replace (update) it
            # Extract resourceVersion from the existing route
            resource_version = existing_route['metadata']['resourceVersion']
            # Include the resourceVersion in your manifest to clear log for resource_version missing 
            httproute_manifest['metadata']['resourceVersion'] = resource_version
            response = api_instance.replace_namespaced_custom_object(group=group, version=version, namespace=namespace, name=ingress_name, plural=plural, body=httproute_manifest)
            logger.info(f"HTTPRoute '{ingress_name}' updated in namespace '{namespace}'.")
            httproute_uid = meta.get('uid')
            print(httproute_uid)
            return True
        except ApiException as e:
            if e.status == 404:
                # If it doesn't exist, create it
                response = api_instance.create_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, body=httproute_manifest)
                logger.info(f"HTTPRoute '{ingress_name}' created in namespace '{namespace}'.")
                httproute_uid = meta.get('uid')
                print(httproute_uid)
                return True 
            else:
                # Handle other exceptions
                logger.error(f"API exception when accessing HTTPRoute: {e}")
                #raise
                return False
    except ApiException as e:
        logger.error(f"Failed to create or update HTTPRoute '{ingress_name}': {e}")
        #raise kopf.TemporaryError(f"Exception when calling CustomObjectsApi for HTTPRoute '{ingress_name}': {e}", delay=30)
        return False


def manage_ratelimit(spec, name, namespace, meta, logger, **kwargs):
    global httproute_uid
    print(httproute_uid)
    # Check if rate limiting is enabled in the CRD spec
    rate_limit_config = spec.get('rateLimit', {})
    if not rate_limit_config.get('enabled', False):
        logger.info(f"Rate limiting not enabled for '{name}'. Plugin creation skipped.")
        return

    api_instance = kubernetes.client.CustomObjectsApi()
    plugin_name = f"rate-limit-{name}"
    namespace = "istio-ingress"  # This namespace can be adjusted to where your plugins should be deployed

    group = "configuration.konghq.com"  # API group for Kong plugins
    version = "v1"  # Version for Kong plugins
    plural = "kongplugins"  # The plural name of the KongPlugin resource
    rate_limit_config_interval = int(rate_limit_config['limit'])
    #rate_limit_config_interval=int(rate_limit_config_interval)
    print(rate_limit_config_interval)
    
    # sample print{'enabled': True, 'identifier': 'IP', 'interval': 'pm', 'limit': '5'}

    rate_limit_plugin_manifest = {
        "apiVersion": f"{group}/{version}",
        "kind": "KongPlugin",
        "metadata": {
            "name": plugin_name,
            "namespace": namespace
        },
        "config": {
            "minute": rate_limit_config_interval,
            "policy": "local"
        },
        "plugin": "rate-limiting"
    }

    try:
        # Check if the plugin already exists
        existing_plugin = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=plugin_name)
        resource_version = existing_plugin['metadata']['resourceVersion']
        rate_limit_plugin_manifest['metadata']['resourceVersion'] = resource_version
        response = api_instance.replace_namespaced_custom_object(group=group, version=version, namespace=namespace, name=plugin_name, plural=plural, body=rate_limit_plugin_manifest)
        logger.info(f"Rate Limiting Plugin '{plugin_name}' updated in namespace '{namespace}'.")
        return plugin_name  # Return plugin name on success
    except ApiException as e:
        if e.status == 404:
            response = api_instance.create_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, body=rate_limit_plugin_manifest)
            logger.info(f"Rate Limiting Plugin '{plugin_name}' created in namespace '{namespace}'.")
            return plugin_name  # Return plugin name on success
        else:
            logger.error(f"API exception when accessing KongPlugin: {e}")
            raise


def manage_apiauthentication(spec, name, namespace, meta, logger, **kwargs):
    global httproute_uid
    print(httproute_uid)
    # Check if api authentication is enabled in the CRD spec
    apiauthentication_config = spec.get('apiKeyVerification', {})
    print(apiauthentication_config)
    if not apiauthentication_config.get('enabled', False):
        logger.info(f"Rate authentication not enabled for '{name}'. Plugin creation skipped.")
        return

    api_instance = kubernetes.client.CustomObjectsApi()
    plugin_name = f"apiauthentication-{name}"
    namespace = "istio-ingress"  # This namespace can be adjusted to where your plugins should be deployed
    
    group = "configuration.konghq.com"  # API group for Kong plugins
    version = "v1"  # Version for Kong plugins
    plural = "kongplugins"  # The plural name of the KongPlugin resource
    #rate_limit_config_interval = int(rate_limit_config['limit'])
    #rate_limit_config_interval=int(rate_limit_config_interval)
    #print(rate_limit_config_interval)


    apiauthentication = {
        "apiVersion": f"{group}/{version}",
        "kind": "KongPlugin",
        "metadata": {
            "name": plugin_name,
            "namespace": namespace
        },
        "config": {
            #  static config values , change in crd required to make dynamic

            "uri_param_names": ["jwt"],
            "cookie_names": ["token"],
            "claims_to_verify": ["exp"],
            "key_claim_name": "iss",
            "secret_is_base64": False,

        },
        "plugin": "jwt"
    }

    try:
        # Check if the plugin already exists
        existing_plugin = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=plugin_name)
        resource_version = existing_plugin['metadata']['resourceVersion']
        apiauthentication['metadata']['resourceVersion'] = resource_version
        response = api_instance.replace_namespaced_custom_object(group=group, version=version, namespace=namespace, name=plugin_name, plural=plural, body=apiauthentication)
        logger.info(f"api authentication '{plugin_name}' updated in namespace '{namespace}'.")
        return plugin_name  # Return plugin name on success
    except ApiException as e:
        if e.status == 404:
            response = api_instance.create_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, body=apiauthentication)
            logger.info(f"api authentication '{plugin_name}' created in namespace '{namespace}'.")
            return plugin_name  # Return plugin name on success
        else:
            logger.error(f"API exception when accessing KongPlugin: {e}")
            raise

def manage_cors(spec, name, namespace, meta, logger, **kwargs):
    global httproute_uid
    
    # Check if CORS is enabled in the CRD spec
    cors_config = spec.get('CORS', {})
    print(cors_config)
    if not cors_config.get('enabled', False):
        logger.info(f"CORS not enabled for '{name}'. Configuration skipped.")
        return

    api_instance = kubernetes.client.CustomObjectsApi()
    plugin_name = f"cors-{name}"
    namespace = "istio-ingress"  # Adjust to your desired namespace for deployment

    group = "configuration.konghq.com"  # API group for CORS plugins (assuming using Kong or similar)
    version = "v1"  # Version for the configuration
    plural = "kongplugins"  # The plural name of the KongPlugin resource

    cors_plugin_manifest = {
        "apiVersion": f"{group}/{version}",
        "kind": "KongPlugin",
        "metadata": {
            "name": plugin_name,
            "namespace": namespace
        },
        "config": {
            "methods": cors_config.get('handlePreflightRequests', {}).get('allowMethods', ["GET", "POST", "HEAD", "OPTIONS"]).split(", "),
            "headers": cors_config.get('handlePreflightRequests', {}).get('allowHeaders', ["Origin", "Accept", "X-Requested-With", "Content-Type", "Access-Control-Request-Method", "Access-Control-Request-Headers"]).split(", "),
            "origins": [cors_config.get('allowOrigins', "*")],
            "credentials": cors_config.get('allowCredentials', False),
            "max_age": cors_config.get('handlePreflightRequests', {}).get('maxAge', 3600)
        },
        "plugin": "cors"
    }

    try:
        # Check if the plugin already exists
        existing_plugin = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=plugin_name)
        resource_version = existing_plugin['metadata']['resourceVersion']
        cors_plugin_manifest['metadata']['resourceVersion'] = resource_version
        response = api_instance.replace_namespaced_custom_object(group=group, version=version, namespace=namespace, name=plugin_name, plural=plural, body=cors_plugin_manifest)
        logger.info(f"CORS Plugin '{plugin_name}' updated in namespace '{namespace}'.")
        return plugin_name  # Return plugin name on success
    except ApiException as e:
        if e.status == 404:
            # If it doesn't exist, create it
            response = api_instance.create_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, body=cors_plugin_manifest)
            logger.info(f"CORS Plugin '{plugin_name}' created in namespace '{namespace}'.")
            return plugin_name  # Return plugin name on success
        else:
            logger.error(f"API exception when accessing CORS Plugin: {e}")
            raise


def update_httproute_annotations(name, namespace, annotations, logger):
    api_instance = kubernetes.client.CustomObjectsApi()
    ingress_name = f"kong-api-route-{name}"
    group = "gateway.networking.k8s.io"
    version = "v1"
    plural = "httproutes"
    namespace = "istio-ingress"

    try:
        # Fetch the current HTTPRoute to get the resourceVersion
        current_route = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=ingress_name)
        resource_version = current_route['metadata']['resourceVersion']

        # Update with new annotations
        api_instance.patch_namespaced_custom_object(
            group=group, 
            version=version, 
            namespace=namespace, 
            plural=plural, 
            name=ingress_name, 
            body={
                "metadata": {
                    "annotations": annotations,
                    "resourceVersion": resource_version
                }
            }
        )
        logger.info(f"HTTPRoute '{ingress_name}' updated with annotations in namespace '{namespace}'.")
        return True
    except ApiException as e:
        logger.error(f"Failed to update annotations for HTTPRoute '{ingress_name}': {e}")
        return False









def check_url(url):
    """ Check if the URL is accessible. """
    try:
        response = requests.head(url)
        response.raise_for_status()  # Will raise an HTTPError if the HTTP request returned an unsuccessful status code
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to reach the URL: {url}. Error: {e}")
        return False

def download_template(url):
    """ Download and parse the YAML or JSON template from the URL. """
    try:
        response = requests.get(url)
        response.raise_for_status()
        documents = yaml.safe_load_all(response.text)  # Handle multiple YAML documents
        return list(documents)  # Convert generator to list if you need to process multiple documents
    except requests.RequestException as e:
        logger.error(f"Failed to download the template from: {url}. Error: {e}")
        return None




def apply_plugins_from_template(templates, namespace):
    """ Apply the configurations from the template to the Kubernetes cluster. """
    namespace = "istio-ingress"
    plugin_names = []
    api_instance = kubernetes.client.CustomObjectsApi()


    for template in templates:
        plugin_name = template['metadata']['name']
        group, version = template['apiVersion'].split('/')
        plural = 'kongplugins'  # Modify as necessary based on the resource type

        try:
            # Attempt to get the existing plugin
            existing_plugin = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=plugin_name)
            resource_version = existing_plugin['metadata']['resourceVersion']
            template['metadata']['resourceVersion'] = resource_version  # Needed to update the resource
            response = api_instance.replace_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=plugin_name, body=template)
            logger.info(f"Plugin '{plugin_name}' updated in namespace '{namespace}'.")
        except ApiException as e:
            if e.status == 404:
                # If the plugin does not exist, create it
                response = api_instance.create_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, body=template)
                logger.info(f"Plugin '{plugin_name}' created in namespace '{namespace}'.")
            else:
                # Log other exceptions
                logger.error(f"Failed to apply plugin '{plugin_name}': {e}")
                continue  # Skip to next plugin

        plugin_names.append(plugin_name)
    return plugin_names


def manage_plugins_from_url(spec, name, namespace, meta, logger):
    """ Manages downloading and applying plugins from a URL specified in the CRD. """
    plugin_names = []
    template_url = spec.get('template')
    if check_url(template_url):
        templates = download_template(template_url)
        if templates:
            plugin_names.extend(apply_plugins_from_template(templates, namespace))
            logger.info(f"Plugins applied from URL and their names collected: {plugin_names}")
        else:
            logger.info("Template download failed or was empty.")
    else:
        logger.info("Template URL is not reachable.")
    return plugin_names

















"""
def fetch_template_from_url(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        if url.endswith('.yaml') or url.endswith('.yml'):
            # Process multiple documents in a single YAML file
            documents = list(yaml.safe_load_all(response.text))
            if documents:
                return documents[0]  # Assuming you want to use the first document
            else:
                logger.error("No documents found in the YAML file.")
                return None
        else:
            return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch template from URL '{url}': {str(e)}")
        return None
    except yaml.YAMLError as ye:
        logger.error(f"Failed to parse YAML from URL '{url}': {str(ye)}")
        return None

def fetch_template_from_k8s(name, namespace, api_instance):
    try:
        group = 'oda.tmforum.org'
        version = 'v1beta3'
        plural = 'kongplugins'  # Assuming the template resources are under this plural
        template = api_instance.get_namespaced_custom_object(group=group, version=version, namespace=namespace, plural=plural, name=name)
        return template.get('spec', {})
    except ApiException as e:
        logger.error(f"Failed to fetch template '{name}' from Kubernetes: {e}")
        return None

def apply_template_to_spec(spec, template_ref, namespace, api_instance):
    if template_ref.startswith('http://') or template_ref.startswith('https://'):
        template = fetch_template_from_url(template_ref)
    else:
        template = fetch_template_from_k8s(template_ref, namespace, api_instance)

    if not template:
        logger.error("Template is not found or invalid. Proceeding without template settings.")
        return spec

    # Ensure spec is mutable
    modified_spec = dict(spec)  # Create a mutable copy if necessary

    # Merge the template settings into the spec
    for key, value in template.items():
        if key not in modified_spec:
            modified_spec[key] = value
        elif isinstance(value, dict) and isinstance(modified_spec[key], dict):
            modified_spec[key].update(value)

    return modified_spec









"""

















"""


def annotate_with_ratelimit(spec):
    annotations = {}
    rate_limit = spec.get('rateLimit', {})
    if rate_limit.get('enabled'):
        # Assume 'limit' is mandatory when 'enabled' is True; add more checks as necessary
        limit = rate_limit.get('limit')
        # Construct Kong plugin configuration for rate limiting
        # This example constructs a basic configuration; adjust according to your Kong version and needs
        annotations['konghq.com/plugins'] = json.dumps({
            "name": "rate-limiting",
            "config": {
                "minute": limit,  # Using 'minute' as an example; adjust according to 'interval' in your spec
                # Add more Kong plugin configuration as necessary
            }
        })
    return annotations




def annotate_with_quota(spec):
    annotations = {}
    quota = spec.get('quota', {})
    if quota.get('enabled'):
        limit = quota.get('limit')
        annotations['konghq.com/quota-plugin'] = json.dumps({
            "name": "quota-plugin",
            "config": {
                "limit": limit,
                # Add more configuration as necessary
            }
        })
    return annotations


def generate_annotations(spec):
    annotations = {}
    annotations.update(annotate_with_ratelimit(spec))
    annotations.update(annotate_with_quota(spec))
    # Add more annotation functions as needed
    return annotations


"""


