var SnSourceConstants = Class.create();
SnSourceConstants.prototype = {
    initialize: function() {},

    API_VERSION: 'v1',
    MAX_PAGE_SIZE: 200,
    DEFAULT_PAGE_SIZE: 100,
    MAX_CONTEXT_GROUPS_PER_TYPE: 1000,
    MAX_EXPORT_ITEMS: 100,
    MAX_CHANGE_ITEMS: 50,
    MAX_BODY_CHARS: 524288,
    MAX_FIELD_CHARS: 262144,
    MAX_DIAGNOSTIC_MINUTES: 60,

    roles: {
        export: 'x_snagentic_source.export',
        diagnostics: 'x_snagentic_source.diagnostics',
        validate: 'x_snagentic_source.validate',
        dev_import: 'x_snagentic_source.dev_import'
    },

    capabilityCategories: [
        'native_source_control',
        'managed_bidirectional',
        'export_only',
        'diagnostic_only',
        'excluded'
    ],

    capabilities: [
        {
            key: 'servicenow_source_control',
            category: 'native_source_control',
            description: 'ServiceNow-managed application source control and packaging metadata'
        },
        {
            key: 'change_bundle',
            category: 'managed_bidirectional',
            description: 'Validated development-only create, update, and delete operations'
        },
        {
            key: 'artifact_export',
            category: 'export_only',
            description: 'Allowlisted configuration inventory and content export'
        },
        {
            key: 'bounded_diagnostics',
            category: 'diagnostic_only',
            description: 'Redacted configuration and aggregate operational diagnostics'
        },
        {
            key: 'sensitive_and_business_data',
            category: 'excluded',
            description: 'Credentials, encrypted properties, journals, attachments, requests, and unrestricted business records'
        }
    ],

    artifactTypes: {
        script_include: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_script_include',
            nameField: 'name',
            readableFields: [
                'name', 'api_name', 'description', 'active', 'access',
                'client_callable', 'script'
            ],
            writableFields: [
                'name', 'api_name', 'description', 'active', 'access',
                'client_callable', 'script'
            ]
        },
        business_rule: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_script',
            nameField: 'name',
            readableFields: [
                'name', 'description', 'active', 'collection', 'when', 'order',
                'action_insert', 'action_update', 'action_delete', 'action_query',
                'filter_condition', 'script'
            ],
            writableFields: [
                'name', 'description', 'active', 'collection', 'when', 'order',
                'action_insert', 'action_update', 'action_delete', 'action_query',
                'filter_condition', 'script'
            ]
        },
        acl: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_security_acl',
            nameField: 'name',
            readableFields: [
                'name', 'type', 'operation', 'description', 'active',
                'admin_overrides', 'advanced', 'condition', 'script'
            ],
            writableFields: [
                'name', 'type', 'operation', 'description', 'active',
                'admin_overrides', 'advanced', 'condition', 'script'
            ]
        },
        dictionary: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_dictionary',
            nameField: 'element',
            readableFields: [
                'name', 'element', 'column_label', 'internal_type', 'max_length',
                'mandatory', 'read_only', 'active'
            ],
            writableFields: [
                'name', 'element', 'column_label', 'internal_type', 'max_length',
                'mandatory', 'read_only', 'active'
            ]
        },
        client_script: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_script_client',
            nameField: 'name',
            readableFields: [
                'name', 'description', 'table', 'active', 'type', 'ui_type',
                'global', 'isolate_script', 'script'
            ],
            writableFields: [
                'name', 'description', 'table', 'active', 'type', 'ui_type',
                'global', 'isolate_script', 'script'
            ]
        },
        ui_action: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_ui_action',
            nameField: 'name',
            readableFields: [
                'name', 'table', 'active', 'client', 'form_button',
                'list_action', 'order', 'onclick', 'condition', 'script'
            ],
            writableFields: [
                'name', 'table', 'active', 'client', 'form_button',
                'list_action', 'order', 'onclick', 'condition', 'script'
            ]
        },
        ui_policy: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_ui_policy',
            nameField: 'short_description',
            readableFields: [
                'short_description', 'table', 'active', 'global',
                'reverse_if_false', 'on_load', 'order', 'conditions',
                'run_scripts', 'script_true', 'script_false'
            ],
            writableFields: [
                'short_description', 'table', 'active', 'global',
                'reverse_if_false', 'on_load', 'order', 'conditions',
                'run_scripts', 'script_true', 'script_false'
            ]
        },
        scripted_rest_api: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_ws_definition',
            nameField: 'name',
            readableFields: [
                'name', 'service_id', 'short_description', 'active', 'consumes',
                'produces'
            ],
            writableFields: [
                'name', 'service_id', 'short_description', 'active', 'consumes',
                'produces'
            ]
        },
        scripted_rest_resource: {
            capabilityCategory: 'managed_bidirectional',
            table: 'sys_ws_operation',
            nameField: 'name',
            readableFields: [
                'name', 'http_method', 'relative_path', 'short_description',
                'requires_authentication', 'active', 'web_service_definition',
                'operation_script'
            ],
            writableFields: [
                'name', 'http_method', 'relative_path', 'short_description',
                'requires_authentication', 'active', 'web_service_definition',
                'operation_script'
            ]
        },
        scheduled_job: {
            capabilityCategory: 'export_only',
            table: 'sysauto_script',
            nameField: 'name',
            readableFields: [
                'name', 'active', 'run_type', 'run_start', 'run_period',
                'time_zone', 'script'
            ],
            writableFields: []
        },
        notification: {
            capabilityCategory: 'export_only',
            table: 'sysevent_email_action',
            nameField: 'name',
            readableFields: [
                'name', 'active', 'collection', 'event_name', 'order',
                'condition', 'advanced_condition'
            ],
            writableFields: []
        },
        system_property: {
            capabilityCategory: 'export_only',
            table: 'sys_properties',
            nameField: 'name',
            readableFields: [
                'name', 'description', 'type', 'suffix', 'ignore_cache'
            ],
            writableFields: []
        }
    },

    artifactTypeCapabilities: function() {
        var result = [];
        for (var key in this.artifactTypes) {
            if (this.artifactTypes.hasOwnProperty(key)) {
                result.push({
                    key: key,
                    category: this.artifactTypes[key].capabilityCategory
                });
            }
        }
        result.sort(function(left, right) {
            return left.key < right.key ? -1 : (left.key > right.key ? 1 : 0);
        });
        return result;
    },

    forbiddenFieldPattern: /(^|_)(password|passwd|credential|secret|token|api_key|private_key|authorization|auth_header|cookie|session|encryption|encrypted|journal|comments|work_notes|request_body|response_body|attachment)(_|$)/i,

    type: 'SnSourceConstants'
};
