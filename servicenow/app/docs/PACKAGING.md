# ServiceNow-generated metadata

This repository contains portable source text and declarative descriptors, not
a complete ServiceNow application package.

A real instance must generate and own:

- `sys_id` values for the application, scope, roles, Script Includes, REST API,
  resources, ACLs, properties, and tombstone table/dictionary records
- `sys_update_name`, `sys_package`, `sys_scope`, customer-update, and
  source-control tracking records
- Script Include dependency/access metadata
- explicit cross-scope privilege records for every allowlisted platform table
  accessed by the application, reviewed and generated in the target instance
- Scripted REST API/resource records and route ordering
- the `GET /contexts` Scripted REST resource record and its export-role ACL
- role containment and ACL records
- dictionary records, indexes, labels, and application access for the custom
  tombstone table
- update-set XML or ServiceNow source-control files used for deployment

Do not hand-copy placeholder identifiers into an instance. Create/import the
records in the target scope, replace descriptor references with generated
identifiers where necessary, test ACL/cross-scope behavior, and export using
ServiceNow-supported packaging.
