// SPDX-License-Identifier: Apache-2.0

/**
 * OpenAEV's asset taxonomy (#11781), lower-cased as the backend's ASSET_TAXONOMY
 * (backend/app/services/organization_context.py) stores it: the category and
 * subcategory an asset row of the organization profile carries, and their
 * words. dev-docs/asset-model.md maps it across OpenAEV, OpenCRQ and OpenCTI.
 */

/** @i18n-keys */
export const ASSET_CATEGORY_LABELS = {
  host: "Host",
  container_workload: "Container workload",
  cloud_resource: "Cloud resource",
  web_application: "Web application",
  network_device: "Network device",
  mobile_device: "Mobile device",
  iot_ot_device: "IoT or OT device",
  identity: "Identity (asset category)",
  saas_application: "SaaS application",
  ai_target: "AI system (asset category)",
  security_platform: "Security platform",
  source_code: "Source code",
  generic_asset: "Other asset (to classify)",
} as const;

export type AssetCategory = keyof typeof ASSET_CATEGORY_LABELS;

/** @i18n-keys */
const ASSET_SUBCATEGORY_LABELS = {
  server: "Server",
  workstation: "Workstation",
  laptop: "Laptop",
  virtual_machine: "Virtual machine",
  hypervisor: "Hypervisor",
  mainframe: "Mainframe",
  thin_client: "Thin client",
  container: "Container",
  container_image: "Container image",
  kubernetes_pod: "Kubernetes pod",
  kubernetes_cluster: "Kubernetes cluster",
  kubernetes_node: "Kubernetes node",
  serverless_function: "Serverless function",
  compute: "Cloud compute",
  storage: "Cloud storage",
  database: "Database (asset subcategory)",
  networking: "Cloud networking",
  serverless: "Serverless service",
  container_registry: "Container registry",
  kubernetes: "Managed Kubernetes",
  iam_principal: "IAM principal",
  secrets_key_mgmt: "Secrets and key management",
  messaging_queue: "Messaging queue",
  analytics_data: "Analytics and data",
  ai_ml_service: "AI or ML service",
  iac_template: "Infrastructure as code template",
  cloud_other: "Other cloud resource",
  website: "Website",
  web_api: "Web API",
  single_page_app: "Single-page application",
  graphql_api: "GraphQL API",
  web_service: "Web service",
  microservice: "Microservice",
  router: "Router",
  switch: "Network switch",
  firewall: "Firewall",
  load_balancer: "Load balancer",
  vpn_gateway: "VPN gateway",
  wireless_ap: "Wireless access point",
  proxy: "Proxy (asset subcategory)",
  dns_server: "DNS server",
  dhcp_server: "DHCP server",
  san_nas: "SAN or NAS storage",
  network_other: "Other network device",
  smartphone: "Smartphone",
  tablet: "Tablet",
  iot_sensor: "IoT sensor",
  ip_camera: "IP camera",
  gateway: "IoT gateway",
  point_of_sale: "Point of sale",
  media_device: "Media device",
  plc: "PLC",
  rtu: "RTU",
  hmi: "HMI",
  scada_historian: "SCADA historian",
  medical_device: "Medical device",
  printer_peripheral: "Printer or peripheral",
  building_mgmt: "Building management system",
  user_account: "User account",
  service_account: "Service account (asset subcategory)",
  group: "Identity group",
  role: "Identity role",
  shared_mailbox: "Shared mailbox",
  non_human_identity: "Non-human identity",
  saas_app: "SaaS app",
  saas_tenant: "SaaS tenant",
  llm_model: "LLM model",
  ai_agent: "AI agent (asset subcategory)",
  mcp_server: "MCP server (asset subcategory)",
  rag_pipeline: "RAG pipeline",
  edr: "EDR",
  xdr: "XDR",
  siem: "SIEM",
  soar: "SOAR",
  ndr: "NDR",
  ispm: "ISPM",
  email_security: "Email security",
  llm_firewall: "LLM firewall",
  ai_gateway: "AI gateway",
  vulnerability_scanner: "Vulnerability scanner",
  code_repository: "Code repository",
  ci_cd_pipeline: "CI/CD pipeline",
  package_registry: "Package registry",
  software_package: "Software package",
  code_other: "Other code asset",
} as const;

type AssetSubcategory = keyof typeof ASSET_SUBCATEGORY_LABELS;

/** Category -> its subcategories, in OpenAEV's order (the backend's ASSET_TAXONOMY). */
const ASSET_TAXONOMY: Record<AssetCategory, readonly AssetSubcategory[]> = {
  host: ["server", "workstation", "laptop", "virtual_machine", "hypervisor", "mainframe", "thin_client"],
  container_workload: ["container", "container_image", "kubernetes_pod", "kubernetes_cluster", "kubernetes_node", "serverless_function"],
  cloud_resource: [
    "compute",
    "storage",
    "database",
    "networking",
    "serverless",
    "container_registry",
    "kubernetes",
    "iam_principal",
    "secrets_key_mgmt",
    "messaging_queue",
    "analytics_data",
    "ai_ml_service",
    "iac_template",
    "cloud_other",
  ],
  web_application: ["website", "web_api", "single_page_app", "graphql_api", "web_service", "microservice"],
  network_device: ["router", "switch", "firewall", "load_balancer", "vpn_gateway", "wireless_ap", "proxy", "dns_server", "dhcp_server", "san_nas", "network_other"],
  mobile_device: ["smartphone", "tablet"],
  iot_ot_device: ["iot_sensor", "ip_camera", "gateway", "point_of_sale", "media_device", "plc", "rtu", "hmi", "scada_historian", "medical_device", "printer_peripheral", "building_mgmt"],
  identity: ["user_account", "service_account", "group", "role", "shared_mailbox", "non_human_identity"],
  saas_application: ["saas_app", "saas_tenant"],
  ai_target: ["llm_model", "ai_agent", "mcp_server", "rag_pipeline"],
  security_platform: ["edr", "xdr", "siem", "soar", "ndr", "ispm", "email_security", "llm_firewall", "ai_gateway", "vulnerability_scanner"],
  // What the organization builds and ships (#1233); a third-party dependency is the repository's SBOM software, not an asset.
  source_code: ["code_repository", "ci_cd_pipeline", "package_registry", "software_package", "code_other"],
  generic_asset: [],
};

/** OpenAEV's asset criticality, lower-cased. @i18n-keys */
export const ASSET_CRITICALITY_LABELS = {
  unknown: "Criticality unknown",
  low: "Low (criticality)",
  medium: "Medium (criticality)",
  high: "High (criticality)",
  very_high: "Very high (criticality)",
} as const;

/** The value of the type picker: "category" alone, or "category/subcategory". */
export function typeValue(category: string, subcategory: string): string {
  return subcategory ? `${category}/${subcategory}` : category;
}

/** The category and subcategory a type picker value names. */
export function parseTypeValue(value: string): { category: AssetCategory; subcategory: string } {
  const [category, subcategory = ""] = value.split("/");
  return { category: (category in ASSET_CATEGORY_LABELS ? category : "generic_asset") as AssetCategory, subcategory };
}

/** The type picker's options, grouped by category: the category itself first ("any"), then each subcategory. */
export function typeOptions(t: (text: string) => string): { value: string; label: string; group: string; selectedLabel: string }[] {
  return (Object.keys(ASSET_TAXONOMY) as AssetCategory[]).flatMap((category) => {
    const group = t(ASSET_CATEGORY_LABELS[category]);
    return [
      { value: category, label: group, group, selectedLabel: group },
      ...ASSET_TAXONOMY[category].map((sub) => {
        const label = t(ASSET_SUBCATEGORY_LABELS[sub]);
        return { value: typeValue(category, sub), label, group, selectedLabel: `${group} > ${label}` };
      }),
    ];
  });
}

/** The words of one taxonomy value as a platform writes it ("HOST", "SERVER"): its category or subcategory label, else the value. */
export function taxonomyValueLabel(t: (text: string) => string, value: string): string {
  const key = value.toLowerCase();
  if (key in ASSET_CATEGORY_LABELS) return t(ASSET_CATEGORY_LABELS[key as AssetCategory]);
  if (key in ASSET_SUBCATEGORY_LABELS) return t(ASSET_SUBCATEGORY_LABELS[key as AssetSubcategory]);
  return value;
}

/** The words of a row's type: "Category > Subcategory", or the category alone. */
export function typeLabel(t: (text: string) => string, category: string, subcategory: string): string {
  const cat = t(ASSET_CATEGORY_LABELS[category as AssetCategory] ?? category);
  const sub = subcategory ? t(ASSET_SUBCATEGORY_LABELS[subcategory as AssetSubcategory] ?? subcategory) : "";
  return sub ? `${cat} > ${sub}` : cat;
}
