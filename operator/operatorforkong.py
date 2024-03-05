import kopf
import yaml
from kubernetes import client, config

# Load Kubernetes configuration
config.load_kube_config()

# Kubernetes API client
api = client.NetworkingV1Api()
v1 = client.CoreV1Api()

# Function to get service details from the istio-ingress namespace
def get_istio_ingress_service_details(logger):
    try:
        istio_ingress_service = v1.read_namespaced_service(name="istio-ingress", namespace="istio-ingress")
        for port in istio_ingress_service.spec.ports:
            if port.name == 'http2':
                return istio_ingress_service.metadata.name, port.port
        logger.error("The 'http2' port could not be found in  istio-ingress service.")
        return None, None
    except client.exceptions.ApiException as e:
        if e.status == 404:
            logger.error("The istio-ingress service does not exist in istio-ingress namespace.")
        else:
            logger.error(f"An error occurred during fetching istio-ingress service: {e}")
        return None, None

# Ingress template
ingress_template = """
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}-ingress
  namespace: istio-ingress
  annotations:
    kubernetes.io/ingress.class: "kong"
    konghq.com/strip-path: "false"
    konghq.com/plugins: "jwt-auth-in-istio, rate-limit-5-min-istio"
spec:
  rules:
  - http:
      paths:
      - path: /r1-productcatalogmanagement/tmf-api/productCatalogManagement/v4
        pathType: Prefix
        backend:
          service:
            name: {istio_service_name}
            port:
              number: {istio_service_port}
"""

@kopf.on.startup()
def scan_existing_services(logger, **kwargs):
    services = v1.list_namespaced_service(namespace="components").items
    istio_service_name, istio_service_port = get_istio_ingress_service_details(logger)
    if not istio_service_name or not istio_service_port:
        logger.error('Cant get Istio ingress details. Ingress creation can't be done.')
        return
    for svc in services:
        if "productcatalogmanagement" in svc.metadata.name:
            ingress_name = f"{svc.metadata.name}-ingress"
            try:
                api.read_namespaced_ingress(name=ingress_name, namespace="istio-ingress")
                logger.info(f"Ingress for service {svc.metadata.name} already exists.")
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    # Ingress does not exist it will create it
                    create_ingress_for_service(svc.metadata.name, istio_service_name, istio_service_port, logger)

def create_ingress_for_service(service_name, istio_service_name, istio_service_port, logger):
    ingress_body = yaml.safe_load(ingress_template.format(
        name=service_name,
        istio_service_name=istio_service_name,
        istio_service_port=istio_service_port))
    api.create_namespaced_ingress(namespace="istio-ingress", body=ingress_body)
    logger.info(f"Ingress {service_name}-ingress created for service {service_name} in namespace istio-ingress")

@kopf.on.create('services')
def on_service_create(body, meta, spec, logger, **kwargs):
    service_name = meta.get('name')
    namespace = meta.get('namespace')
    if 'productcatalogmanagement' in service_name and namespace == 'components':
        istio_service_name, istio_service_port = get_istio_ingress_service_details(logger)
        if istio_service_name and istio_service_port:
            create_ingress_for_service(service_name, istio_service_name, istio_service_port, logger)
        else:
            logger.error(f'Failed to create Ingress for {service_name} as Istio ingress details could not be obtained.')

@kopf.on.delete('services')
def on_service_delete(meta, logger, **kwargs):
    service_name = meta.get('name')
    namespace = meta.get('namespace')
    if 'productcatalogmanagement' in service_name and namespace == 'components':
        ingress_name = f"{service_name}-ingress"
        try:
            api.delete_namespaced_ingress(name=ingress_name, namespace="istio-ingress")
            logger.info(f"Ingress {ingress_name} deleted from namespace istio-ingress")
        except client.exceptions.ApiException as e:
            logger.error(f"Failed to delete Ingress {ingress_name} from namespace istio-ingress: {e}")
