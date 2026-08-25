# Security

Do not open public issues containing runtime configuration, decrypted data,
provider responses, credentials, or private identities.

Relay URIs are credentials. Store them only in the `EGRESS_PROXY_URI` repository
secret. The runner reads this value from its environment, writes client material
only inside its restrictive temporary directory, suppresses client output, and
removes the URI before starting provider code.

The public repository is designed to contain only provider-neutral source code,
public recipients, encrypted payloads, and opaque ciphertext filenames. The
scheduled job fails closed when the public-tree guard finds a private marker,
credential pattern, local absolute path, or non-English source text in file
contents or relative paths.

Rotate a runtime identity by decrypting all runtime payloads locally,
re-encrypting them to a new recipient, and replacing the corresponding
repository secret. Export identities are intentionally offline and are never
available to the workflow.
