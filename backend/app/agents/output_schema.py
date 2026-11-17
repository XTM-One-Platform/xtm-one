# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Pydantic models for built-in agent output schemas.

Each schema is converted to JSON Schema via ``.model_json_schema()``
and set as the agent's ``output_schema`` to constrain LLM output
through native structured output and Schema-Aligned Parsing (SAP).
"""

import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_SCHEMA_DIR = Path(__file__).parent / "seed_data" / "agent_output_schema"


def cti_stix_transformer_schema() -> dict:
    """STIX 2.1 bundle output schema for the CTI STIX Transformer.

    Reuses the harvester's STIX 2.1 bundle schema (``cti-stix-harvester``) as
    the single source of truth so both agents enforce the exact same STIX
    structure, but relaxes the top-level ``objects.minItems`` from 1 to 0.

    Rationale: a transform that legitimately removes all matching objects
    yields an empty bundle, which the harvester's ``minItems = 1`` would
    reject. Every nested per-object constraint is left untouched.
    """
    schema: dict = json.loads(
        (_SCHEMA_DIR / "cti-stix-harvester.json").read_text(encoding="utf-8")
    )
    objects = schema.get("properties", {}).get("objects")
    if isinstance(objects, dict) and "minItems" in objects:
        objects["minItems"] = 0
    return schema


# ═══════════════════════════════════════════════════════════════════════════════
# NLQ Output Schema
# ═══════════════════════════════════════════════════════════════════════════════

class NlqFilterMode(StrEnum):
    AND = "and"
    OR = "or"


class NlqFilterOperator(StrEnum):
    CONTAINS = "contains"
    ENDS_WITH = "ends_with"
    EQ = "eq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    MATCH = "match"
    NIL = "nil"
    NOT_CONTAINS = "not_contains"
    NOT_ENDS_WITH = "not_ends_with"
    NOT_EQ = "not_eq"
    NOT_NIL = "not_nil"
    NOT_STARTS_WITH = "not_starts_with"
    SCRIPT = "script"
    SEARCH = "search"
    STARTS_WITH = "starts_with"
    WILDCARD = "wildcard"
    WITHIN = "within"


class NlqRelationshipSmallEnum(StrEnum):
    ATTRIBUTED_TO = "attributed-to"
    EXPLOITS = "exploits"
    HAS = "has"
    INDICATES = "indicates"
    LOCATED_AT = "located-at"
    ORIGINATES_FROM = "originates-from"
    PART_OF = "part-of"
    RELATED_TO = "related-to"
    SUBTECHNIQUE_OF = "subtechnique-of"
    TARGETS = "targets"
    USES = "uses"


class NlqEntityObservableEnum(StrEnum):
    ADMINISTRATIVE_AREA = "Administrative-Area"
    ATTACK_PATTERN = "Attack-Pattern"
    CAMPAIGN = "Campaign"
    CHANNEL = "Channel"
    CITY = "City"
    COUNTRY = "Country"
    COURSE_OF_ACTION = "Course-Of-Action"
    DATA_COMPONENT = "Data-Component"
    DATA_SOURCE = "Data-Source"
    EVENT = "Event"
    FEEDBACK = "Feedback"
    GROUPING = "Grouping"
    INCIDENT = "Incident"
    CASE_INCIDENT = "Case-Incident"
    INDICATOR = "Indicator"
    INDIVIDUAL = "Individual"
    INFRASTRUCTURE = "Infrastructure"
    INTRUSION_SET = "Intrusion-Set"
    LANGUAGE = "Language"
    MALWARE = "Malware"
    MALWARE_ANALYSIS = "Malware-Analysis"
    NARRATIVE = "Narrative"
    NOTE = "Note"
    OBSERVED_DATA = "Observed-Data"
    OPINION = "Opinion"
    ORGANIZATION = "Organization"
    POSITION = "Position"
    REGION = "Region"
    REPORT = "Report"
    STIX_CYBER_OBSERVABLE = "Stix-Cyber-Observable"
    CASE_RFI = "Case-Rfi"
    CASE_RFT = "Case-Rft"
    SECTOR = "Sector"
    SYSTEM = "System"
    TASK = "Task"
    THREAT_ACTOR_GROUP = "Threat-Actor-Group"
    THREAT_ACTOR_INDIVIDUAL = "Threat-Actor-Individual"
    TOOL = "Tool"
    VULNERABILITY = "Vulnerability"
    ARTIFACT = "Artifact"
    AUTONOMOUS_SYSTEM = "Autonomous-System"
    BANK_ACCOUNT = "Bank-Account"
    CREDENTIAL = "Credential"
    CRYPTOCURRENCY_WALLET = "Cryptocurrency-Wallet"
    CRYPTOGRAPHIC_KEY = "Cryptographic-Key"
    DIRECTORY = "Directory"
    DOMAIN_NAME = "Domain-Name"
    EMAIL_ADDR = "Email-Addr"
    EMAIL_MESSAGE = "Email-Message"
    EMAIL_MIME_PART_TYPE = "Email-Mime-Part-Type"
    STIX_FILE = "StixFile"
    HOSTNAME = "Hostname"
    IPV4_ADDR = "IPv4-Addr"
    IPV6_ADDR = "IPv6-Addr"
    MAC_ADDR = "Mac-Addr"
    MEDIA_CONTENT = "Media-Content"
    MUTEX = "Mutex"
    NETWORK_TRAFFIC = "Network-Traffic"
    PAYMENT_CARD = "Payment-Card"
    PERSONA = "Persona"
    PHONE_NUMBER = "Phone-Number"
    PROCESS = "Process"
    SOFTWARE = "Software"
    TEXT = "Text"
    TRACKING_NUMBER = "Tracking-Number"
    URL = "Url"
    USER_ACCOUNT = "User-Account"
    USER_AGENT = "User-Agent"
    WINDOWS_REGISTRY_KEY = "Windows-Registry-Key"
    WINDOWS_REGISTRY_VALUE_TYPE = "Windows-Registry-Value-Type"
    X509_CERTIFICATE = "X509-Certificate"
    SSH_KEY = "SSH-Key"


class NlqFilterSmallEnum(StrEnum):
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    CREATED = "created"
    CREATOR_ID = "creator_id"
    CREATEDBY = "createdBy"
    WORKFLOW_ID = "workflow_id"
    OBJECTS = "objects"
    NAME = "name"
    ALIAS = "alias"
    DESCRIPTION = "description"
    ATTRIBUTE_ABSTRACT = "attribute_abstract"
    CONTENT = "content"
    NOTE_TYPES = "note_types"
    X_OPENCTI_DESCRIPTION = "x_opencti_description"
    X_OPENCTI_ADDITIONAL_NAMES = "x_opencti_additional_names"
    MEDIA_CATEGORY = "media_category"
    TITLE = "title"
    X_MITRE_PLATFORMS = "x_mitre_platforms"
    X_MITRE_DETECTION = "x_mitre_detection"
    X_MITRE_ID = "x_mitre_id"
    KILL_CHAIN_PHASES = "killChainPhases"
    FIRST_SEEN = "first_seen"
    LAST_SEEN = "last_seen"
    FIRST_OBSERVED = "first_observed"
    LAST_OBSERVED = "last_observed"
    VALID_FROM = "valid_from"
    VALID_UNTIL = "valid_until"
    PATTERN_TYPE = "pattern_type"
    PATTERN = "pattern"
    INDICATOR_TYPES = "indicator_types"
    PUBLISHED = "published"
    PUBLICATION_DATE = "publication_date"
    DUE_DATE = "due_date"
    SUBMITTED = "submitted"
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_ENDED = "analysis_ended"
    REPORT_TYPES = "report_types"
    OBJECT_PARTICIPANT = "objectParticipant"
    OBJECT_ORGANIZATION = "objectOrganization"
    PERSONA_NAME = "persona_name"
    PERSONA_TYPE = "persona_type"
    X_OPENCTI_ORGANIZATION_TYPE = "x_opencti_organization_type"
    INFRASTRUCTURE_TYPES = "infrastructure_types"
    GOALS = "goals"
    RESOURCE_LEVEL = "resource_level"
    NETWORK_SRC = "networkSrc"
    NETWORK_DST = "networkDst"
    PROTOCOLS = "protocols"
    SRC_PORT = "src_port"
    DST_PORT = "dst_port"
    SRC_PACKETS = "src_packets"
    DST_PACKETS = "dst_packets"
    SRC_PAYLOAD = "srcPayload"
    DST_PAYLOAD = "dstPayload"
    MALWARE_TYPES = "malware_types"
    IS_FAMILY = "is_family"
    IMPLEMENTATION_LANGUAGES = "implementation_languages"
    CAPABILITIES = "capabilities"
    SAMPLES = "samples"
    OPERATING_SYSTEMS = "operatingSystems"
    THREAT_ACTOR_TYPES = "threat_actor_types"
    ROLES = "roles"
    SOPHISTICATION = "sophistication"
    TOOL_TYPES = "tool_types"
    TOOL_VERSION = "tool_version"
    PERSONAL_MOTIVATIONS = "personal_motivations"
    PRIMARY_MOTIVATION = "primary_motivation"
    SECONDARY_MOTIVATIONS = "secondary_motivations"
    X_OPENCTI_CVSS_BASE_SCORE = "x_opencti_cvss_base_score"
    X_OPENCTI_CVSS_BASE_SEVERITY = "x_opencti_cvss_base_severity"
    X_OPENCTI_CVSS_ATTACK_VECTOR = "x_opencti_cvss_attack_vector"
    X_OPENCTI_CVSS_INTEGRITY_IMPACT = "x_opencti_cvss_integrity_impact"
    X_OPENCTI_CVSS_AVAILABILITY_IMPACT = "x_opencti_cvss_availability_impact"
    X_OPENCTI_CVSS_CONFIDENTIALITY_IMPACT = "x_opencti_cvss_confidentiality_impact"
    X_OPENCTI_CISA_KEV = "x_opencti_cisa_kev"
    X_OPENCTI_EPSS_SCORE = "x_opencti_epss_score"
    X_OPENCTI_EPSS_PERCENTILE = "x_opencti_epss_percentile"
    X_OPENCTI_DETECTION = "x_opencti_detection"
    X_OPENCTI_MAIN_OBSERVABLE_TYPE = "x_opencti_main_observable_type"
    X_OPENCTI_SCORE = "x_opencti_score"
    INCIDENT_TYPE = "incident_type"
    SEVERITY = "severity"
    SOURCE = "source"
    CHANNEL_TYPES = "channel_types"
    EVENT_TYPES = "event_types"
    START_TIME = "start_time"
    STOP_TIME = "stop_time"
    PRIORITY = "priority"
    RESPONSE_TYPES = "response_types"
    INFORMATION_TYPES = "information_types"
    TAKEDOWN_TYPES = "takedown_types"
    RESULT = "result"
    REVOKED = "revoked"
    DATE_OF_BIRTH = "date_of_birth"
    GENDER = "gender"
    JOB_TITLE = "job_title"
    MARITAL_STATUS = "marital_status"
    EYE_COLOR = "eye_color"
    HAIR_COLOR = "hair_color"
    BORN_IN = "bornIn"
    ETHNICITY = "ethnicity"
    PATH = "path"
    SIZE = "size"
    SERVICE_NAME = "service_name"
    DESCRIPTIONS = "descriptions"
    GROUP_NAME = "group_name"
    START_TYPE = "start_type"
    SERVICE_TYPE = "service_type"
    SERVICE_STATUS = "service_status"
    CPE = "cpe"
    LANGUAGES = "languages"
    VENDOR = "vendor"
    USER_ID = "user_id"
    CREDENTIAL = "credential"
    ACCOUNT_LOGIN = "account_login"
    ACCOUNT_TYPE = "account_type"
    IS_DISABLED = "is_disabled"
    ACCOUNT_CREATED = "account_created"
    ACCOUNT_EXPIRES = "account_expires"
    ACCOUNT_FIRST_LOGIN = "account_first_login"
    ACCOUNT_LAST_LOGIN = "account_last_login"
    ATTRIBUTE_KEY = "attribute_key"
    MODIFIED_TIME = "modified_time"
    NUMBER_OF_SUBKEYS = "number_of_subkeys"
    IBAN = "iban"
    BIC = "bic"
    ACCOUNT_NUMBER = "account_number"
    CARD_NUMBER = "card_number"
    EXPIRATION_DATE = "expiration_date"
    CVV = "cvv"
    HOLDER_NAME = "holder_name"
    POSTAL_CODE = "postal_code"
    STREET_ADDRESS = "street_address"
    MESSAGE_ID = "message_id"
    SUBJECT = "subject"
    RECEIVED_LINES = "received_lines"
    BODY = "body"
    EMAIL_FROM = "emailFrom"
    EMAIL_SENDER = "emailSender"
    EMAIL_TO = "emailTo"
    EMAIL_CC = "emailCc"
    EMAIL_BCC = "emailBcc"
    BODY_MULTIPART = "bodyMultipart"
    RAW_EMAIL = "rawEmail"
    BODY_RAW = "bodyRaw"
    HASHES_MD5 = "hashes.MD5"
    HASHES_SHA_1 = "hashes.SHA-1"
    HASHES_SHA_256 = "hashes.SHA-256"
    HASHES_SHA_512 = "hashes.SHA-512"
    HASHES_SSDEEP = "hashes.SSDEEP"
    URL = "url"
    EXTENSIONS = "extensions"
    ISSUER = "issuer"
    VALIDITY_NOT_BEFORE = "validity_not_before"
    VALIDITY_NOT_AFTER = "validity_not_after"
    START = "start"
    END = "end"
    IS_ACTIVE = "is_active"
    PID = "pid"
    RIR = "rir"
    DATA = "data"
    DATA_TYPE = "data_type"
    OBJECT_ASSIGNEE = "objectAssignee"
    OBJECT_LABEL = "objectLabel"
    OBJECT_MARKING = "objectMarking"
    EXTERNAL_REFERENCES = "externalReferences"
    OBJECTIVE = "objective"
    VERSION = "version"
    NUMBER = "number"
    VALUE = "value"
    CONTENT_TYPE = "content_type"
    KEY_TYPE = "key_type"
    KEY_PUBLIC = "key_public"
    FINGERPRINT_MD5 = "fingerprint_md5"
    FINGERPRINT_SHA256 = "fingerprint_sha256"


class NlqRegardingOfRelationshipSchema(BaseModel):
    key: Literal["relationship_type"] = Field(
        description="The key of a 'regardingOf' relationship type filter, always 'relationship_type'."
    )
    values: list[NlqRelationshipSmallEnum] = Field(description="A list of relationship type filter values.")


class NlqRegardingOfEntityNameSchema(BaseModel):
    key: Literal["id"] = Field(description="The key of a 'regardingOf' entity name filter, always 'id'.")
    values: list[str] = Field(description="A list of entity name filter values.")


class NlqRegardingOfFilterItem(BaseModel):
    """A filter used to further refine entity filtering based on associated entities and/or relationships."""

    key: Literal["regardingOf"] = Field(description="The key of the 'regardingOf' filter, always 'regardingOf'.")
    values: list[NlqRegardingOfRelationshipSchema | NlqRegardingOfEntityNameSchema] = Field(
        description="A list of entity name or relationship type filter values."
    )
    operator: NlqFilterOperator = Field(description="The operator used to filter results.")
    mode: NlqFilterMode = Field(description="The logical mode (or/and) used to filter results.")


class NlqEntityTypeFilterItem(BaseModel):
    """A filter used to filter entities by their type as defined by the STIX standard."""

    key: Literal["entity_type"] = Field(description="The key of the entity type filter, always 'entity_type'.")
    values: list[NlqEntityObservableEnum] = Field(description="A list of entity type filter values.")
    operator: NlqFilterOperator = Field(description="The operator used to filter results.")
    mode: NlqFilterMode = Field(description="The logical mode (or/and) used to filter results.")


class NlqGenericFilterItem(BaseModel):
    key: NlqFilterSmallEnum = Field(description="The key of the filter.")
    values: list[str] = Field(description="A list of filter values.")
    operator: NlqFilterOperator = Field(description="The operator used to filter results.")
    mode: NlqFilterMode = Field(description="The logical mode (or/and) used to filter results.")


class NlqOutputSchema(BaseModel):
    filters: list[NlqEntityTypeFilterItem | NlqRegardingOfFilterItem | NlqGenericFilterItem] = Field(
        description="The list of filters applied to refine the OpenCTI query."
    )
    mode: NlqFilterMode = Field(description="The logical mode (or/and) used to filter results.")
