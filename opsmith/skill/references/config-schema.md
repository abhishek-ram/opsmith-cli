<!-- Generated from the pydantic models by scripts/build_skill_refs.py. Do not edit. -->
# DeploymentConfig

Describes the deployment config for the repository, listing all services.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `services` | ServiceInfo[] | no |  | A list of services identified in the repository. |
| `infra_deps` | InfrastructureDependency[] | no |  | A list of consolidated infrastructure dependencies required by all services. |
| `app_name` | string | yes |  | The name of the application. |
| `app_name_slug` | string | yes |  | The slugified name of the application. |
| `environments` | DeploymentEnvironment[] | no |  | A list of deployment environments. |

## DependencyTypeEnum

Enum for the different types of infrastructure dependencies.

One of: `DATABASE`, `CACHE`, `MESSAGE_QUEUE`, `SEARCH_ENGINE`

## DeploymentEnvironment

Describes a deployment environment.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes |  | The name of the environment (e.g., 'staging', 'production'). |
| `cloud_provider` | object | yes |  | Cloud provider specific details. |
| `strategy` | string | yes |  | The deployment strategy for this environment. |
| `domain_email` | string | null | no | `None` | The email for SSL certificate registration with services like Let's Encrypt. |
| `domains` | DomainInfo[] | no |  | A list of domain configurations for services. |

## DomainInfo

Describes a domain configuration for a service.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `service_name_slug` | string | yes |  | The slug of the service this domain is for. |
| `domain_name` | string | yes |  | The domain name for the service. |

## EnvVarConfig

Describes an environment variable configuration for a service.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `key` | string | yes |  | The name of the environment variable. |
| `is_secret` | boolean | yes |  | Whether the environment variable should be treated as a secret. |
| `default_value` | string | null | no | `None` | The default value of the environment variable, if present in the code. |

## InfrastructureDependency

Describes an infrastructure dependency for a service.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `dependency_type` | DependencyTypeEnum | yes |  | The type of the infrastructure dependency. |
| `provider` | InfrastructureProviderEnum | yes |  | The specific provider of the dependency. |
| `version` | string | no | `latest` | The version of the infrastructure dependency, if identifiable. |

## InfrastructureProviderEnum

Enum for the different types of infrastructure providers.

One of: `postgresql`, `mysql`, `mongodb`, `redis`, `rabbitmq`, `kafka`, `elasticsearch`, `weaviate`, `user_choice`

## ServiceInfo

Describes a single service to be deployed.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name_slug` | string | no | `slug` | The unique slug for the service, e.g. python_backend_api_1 |
| `language` | string | yes |  | The primary programming language of the service. |
| `language_version` | string | null | no | `None` | The specific version of the language, if identifiable. |
| `service_type` | ServiceTypeEnum | yes |  | The type of the service. |
| `framework` | string | null | no | `None` | The primary framework or library used, if any. |
| `service_port` | integer | null | no | `None` | The port the service listens on, if applicable. |
| `build_cmd` | string | null | no | `None` | The command to build the service, if applicable (e.g., 'npm run build'). This is required for FRONTEND services. |
| `build_dir` | string | null | no | `None` | The directory where build artifacts are located, relative to the repository root (e.g., 'frontend/dist'). This is required for FRONTEND services. |
| `build_path` | string | null | no | `None` | The path to be added to the PATH environment variable so that dependencies for the build are available (e.g., 'node_modules/.bin'). |
| `env_vars` | EnvVarConfig[] | no |  | A list of environment variable configurations required by the service. |

## ServiceTypeEnum

Enum for the different types of services that can be deployed.

One of: `BACKEND_API`, `FRONTEND`, `FULL_STACK`, `BACKEND_WORKER`
