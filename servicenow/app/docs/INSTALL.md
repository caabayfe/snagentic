# Installation

No live ServiceNow instance was used to create this skeleton.

1. On a non-production developer instance, create a scoped application:
   - Name: `Snagentic Source Exchange`
   - Scope: `x_snagentic_source`
   - Version: `1.0.0`
2. Create the roles listed in `../manifest/roles.json`.
3. Create server-only Script Includes from `../src/script_includes/`.
   Preserve the file/class names listed in `../manifest/application.json`.
   Do not mark any Script Include client callable.
4. Create a Scripted REST API:
   - Name: `Snagentic Source Exchange`
   - API ID: `source`
   - Namespace: `x_snagentic_source`
5. Create each resource in `../manifest/rest_api.json` and paste the matching
   resource script. Require authentication on every resource.
6. Create the tombstone table described in `../manifest/tables.json`, with
   application access restricted to this scope.
7. Create application properties and ACLs as described in `SETUP.md` and
   `../roles/README.md`.
8. Import or manually create the scoped records on the target developer
   instance, then let ServiceNow generate source-control/update-set metadata.

Do not deploy the change-application resource to production as an enabled
write path. Its code contains an independent fail-closed development gate, but
the resource should also be disabled or denied by ACL outside development.

## Validation without a live instance

The JSON descriptors and schemas can be reviewed in Git. Runtime behavior,
cross-scope privileges, dictionary attributes, ACL evaluation, and generated
record identifiers must be validated on an isolated developer instance before
packaging.
