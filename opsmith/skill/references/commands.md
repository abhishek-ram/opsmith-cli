<!-- Generated from the Typer application by scripts/build_skill_refs.py. Do not edit. -->

# Opsmith commands

Every command accepts the global options below. With `--output json` a command writes exactly one JSON document to stdout and sends all progress to stderr.

**Every command needs a model configured** - `--model` or `OPSMITH_MODEL`, plus a key - except the few that say otherwise below. A model is part of the tool rather than an option, and a command that needs no external tool may still need one.

## Global options

| Flag | Type | Default | Description |
|---|---|---|---|
| `--version` | boolean | `false` | Print the version of Opsmith and exit. |
| `--model` | str | — | The LLM model to be used by the AI Agent, as provider:name. Required unless OPSMITH_MODEL is set or .opsmith.conf.yml names one. |
| `--api-key` | str | — | The API key for the specified model. Required unless the provider's own key variable, such as ANTHROPIC_API_KEY, is set. |
| `--logfire-token` | str | — | Logfire token to be used for logging. If not provided, logs will not be sent to Logfire. |
| `--src-dir` | str | — | Source directory to be used by the command. Defaults to current working directory. |
| `--verbose`, `-v` | boolean | `false` | Enable verbose output. |
| `--output` | `text` \| `json` | `text` | Output mode. 'text' is the usual terminal output; 'json' prints exactly one JSON envelope on stdout and sends all progress to stderr. |
| `--non-interactive` | boolean | `false` | Never prompt; answers come from the answer options, and a question none of them covers stops the run. Implied by --output json and by a stdin that is not a terminal. |
| `--answer` | str, repeatable | — | Inline answer as key=value. Repeatable, and wins over every other source. |
| `--answers` | path | — | YAML file mapping prompt key to value. |
| `--env-file` | path | — | Dotenv file answering envvar.<KEY> prompts, secrets included. |
| `--accept-defaults` | boolean | `false` | Take each question's default instead of stopping on it. Destructive confirmations are excluded. |
| `--wait-timeout` | int | `600` | Seconds a headless run polls an external action, such as a DNS record being created, before stopping so it can be done. |

## Command groups

| Group | What it is for |
|---|---|
| `opsmith agent` | Install the Opsmith skill into your coding harness. |
| `opsmith config` | Inspect and validate the deployment configuration, without touching a cloud. |
| `opsmith dockerfile` | Check the Dockerfiles this repository declares. |
| `opsmith env` | Inspect and create the deployment environments of this repository. |

## Commands

### opsmith agent install

Install the Opsmith skill into the harnesses that read skills.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed. It does not need a model configured either.

**Result:** [`AgentInstallResult`](#agentinstallresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--target` | str | — | Which harness to install for, required: one harness name, or 'auto' for only where a harness is already set up here. Run the command again to install for another. |
| `--scope` | str | `project` | Whether to install into this project or for this user. |
| `--agents-md` | boolean | `false` | Also write the Opsmith block into AGENTS.md, and the import line into CLAUDE.md. |
| `--force` | boolean | `false` | Replace a skill directory that does not identify itself as Opsmith's. |
| `--mcp` | boolean | `false` | Reserved for MCP configuration, which is not written yet. |

### opsmith agent status

Report where the skill is installed, and whether it is the version running.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed. It does not need a model configured either.

**Result:** [`AgentStatusResult`](#agentstatusresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--scope` | str | — | Report on one scope only. Both when not given. |

### opsmith agent uninstall

Remove the Opsmith skill, and only what installing it wrote.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed. It does not need a model configured either.

**Result:** [`AgentUninstallResult`](#agentuninstallresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--target` | str | — | Which harness to remove for. Everything Opsmith installed here, when not given. |
| `--scope` | str | `project` | Whether to remove from this project or from this user's directories. |

### opsmith config schema

Print the schema of the deployment configuration.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`ConfigSchemaResult`](#configschemaresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--format` | `json` \| `markdown` | `json` | Whether to emit the JSON Schema itself or a markdown rendering of it. |

### opsmith config show

Print the deployment configuration, as Opsmith reads it.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`ConfigShowResult`](#configshowresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--file` | path | — | The configuration file to show. Defaults to .opsmith/deployments.yml. |

### opsmith config validate

Validate the deployment configuration.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`ValidateResult`](#validateresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--file` | path | — | The configuration file to validate. Defaults to .opsmith/deployments.yml. |

### opsmith deploy

Deploy the application to a specified environment.

**Needs:** `docker`, `terraform`.

**Result:** one of: [`EnvCreateResult`](#envcreateresult), [`ReleaseResult`](#releaseresult), [`UpdateResult`](#updateresult), [`RunResult`](#runresult), [`DestroyResult`](#destroyresult).

**This command exits with the status of the command it ran**, after writing a successful envelope. Read `ok` in the envelope before reading the exit code.

### opsmith destroy

Destroy an environment and everything it created.

**Needs:** `terraform`.

**Result:** [`DestroyResult`](#destroyresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--env` | str | required | The environment to destroy. |

### opsmith dockerfile validate

Build and run the Dockerfiles this repository declares, without changing them.

**Needs:** `docker`.

**Result:** [`DockerfileValidateResult`](#dockerfilevalidateresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--service` | str | — | The service to check. Every service built from a Dockerfile when not given. |
| `--timeout` | int | — | Seconds to let the container run before counting it as healthy. Default 60. |

### opsmith env create

Create a deployment environment, and deploy it unless told not to.

**Needs:** `docker`, `terraform`.

**Result:** [`EnvCreateResult`](#envcreateresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--name` | str | — | The name of the new environment. |
| `--provider` | str | — | The cloud provider to deploy to, such as AWS or GCP. |
| `--region` | str | — | The region to deploy into. |
| `--strategy` | str | — | The deployment strategy, such as Monolithic. |
| `--project-id` | str | — | The GCP project to deploy into. |
| `--zone` | str | — | The GCP zone to deploy into. |
| `--instance-type` | str | — | The instance type to create, instead of the one suggested. |
| `--domain` | str, repeatable | — | A service's domain, as slug=host. Repeatable. |
| `--domain-email` | str | — | The email SSL certificates are registered with. |
| `--env-var` | str, repeatable | — | A runtime value, as KEY=VALUE. Repeatable. |
| `--build-env` | str, repeatable | — | A frontend's build-time value, as slug:KEY=VALUE. Repeatable. |
| `--no-deploy` | boolean | `false` | Write the environment to the configuration without deploying it. |

### opsmith env list

List the deployment environments this repository declares.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`EnvListResult`](#envlistresult).

### opsmith env plan

Report every answer creating an environment will need, without creating anything.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`EnvPlanResult`](#envplanresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--name` | str | — | The environment being planned. |
| `--provider` | str | — | The cloud provider to deploy to, such as AWS or GCP. |
| `--region` | str | — | The region to deploy into. |
| `--strategy` | str | — | The deployment strategy, such as Monolithic. |
| `--project-id` | str | — | The GCP project to deploy into. |
| `--zone` | str | — | The GCP zone to deploy into. |
| `--instance-type` | str | — | The instance type to create, instead of the one suggested. |
| `--domain` | str, repeatable | — | A service's domain, as slug=host. Repeatable. |
| `--domain-email` | str | — | The email SSL certificates are registered with. |
| `--env-var` | str, repeatable | — | A runtime value, as KEY=VALUE. Repeatable. |
| `--build-env` | str, repeatable | — | A frontend's build-time value, as slug:KEY=VALUE. Repeatable. |
| `--write-answers` | path | — | Write an answers skeleton to this file, to fill in and pass back with --answers. |

### opsmith env status

Report what an environment is running, without contacting the cloud.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`EnvStatusResult`](#envstatusresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--env` | str | required | The environment to report on. |

### opsmith init

Create a deployment configuration for this repository, without scanning it.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`InitResult`](#initresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--app-name` | str | — | The name of the application. Asked for when it is not given. |

### opsmith release

Build the current code and deploy it to an environment.

**Needs:** `docker`, `terraform`.

**Result:** [`ReleaseResult`](#releaseresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--env` | str | required | The environment to release to. |
| `--env-var` | str, repeatable | — | A runtime value, as KEY=VALUE. Repeatable. |
| `--build-env` | str, repeatable | — | A frontend's build-time value, as slug:KEY=VALUE. Repeatable. |

### opsmith repomap

Generates a map of the repository, showing important files and code elements.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** an untyped JSON object.

### opsmith run

Run a one-off command on a deployed service.

Opsmith exits with whatever the command exited with, so this can be used in a script exactly
as the command itself would be.

**Needs:** nothing - it runs on a machine with neither docker nor terraform installed.

**Result:** [`RunResult`](#runresult).

**This command exits with the status of the command it ran**, after writing a successful envelope. Read `ok` in the envelope before reading the exit code.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--env` | str | required | The environment to run the command in. |
| `--service` | str | required | The service to run the command on. |
| `command` (argument) | str | — | The command to run, after a '--'. For example: opsmith run --env dev --service api -- ls -la |

### opsmith setup

Setup the deployment configuration for the repository.
Identifies services, their languages, types, and frameworks.

**Needs:** `docker`, `terraform`.

**Result:** [`SetupResult`](#setupresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--rescan` | boolean | `false` | Re-scan a repository that already has a configuration, instead of leaving it alone. |
| `--accept-detected` | boolean | `false` | Take the detected services and dependencies as they are, without opening an editor to review each one. |

### opsmith update

Reconcile a deployed environment with the configuration as it now stands.

**Needs:** `docker`, `terraform`.

**Result:** [`UpdateResult`](#updateresult).

| Flag | Type | Default | Description |
|---|---|---|---|
| `--env` | str | required | The environment to update. |
| `--domain` | str, repeatable | — | A service's domain, as slug=host. Repeatable. |
| `--domain-email` | str | — | The email SSL certificates are registered with. |
| `--env-var` | str, repeatable | — | A runtime value, as KEY=VALUE. Repeatable. |

## Exit codes

| Exit code | Error codes |
|---|---|
| 0 | success |
| 1 | `INTERNAL` |
| 2 | `INVALID_ARGUMENT`, `INVALID_CONFIG`, `UNKNOWN_ENVIRONMENT`, `UNKNOWN_SERVICE` |
| 3 | `INTERACTION_CANCELLED`, `MISSING_ANSWER` |
| 4 | `ANSIBLE_FAILED`, `DEPLOY_UNHEALTHY`, `DOCKER_FAILED`, `EDIT_REQUIRED`, `TERRAFORM_FAILED` |
| 5 | `CLOUD_CREDENTIALS`, `CLOUD_PERMISSION` |
| 6 | `LLM_GAVE_UP` |
| 8 | `PENDING_ACTION` |

## Error codes

| Code | Exit | Means |
|---|---|---|
| `INVALID_ARGUMENT` | 2 | An argument, option or environment variable holds a value Opsmith cannot use. |
| `INVALID_ARGUMENT` | 2 | The source directory is not inside a git repository. |
| `INVALID_ARGUMENT` | 2 | git is not installed, or is not on the PATH. |
| `INVALID_CONFIG` | 2 | The deployment configuration is missing, unparsable or fails validation. |
| `UNKNOWN_ENVIRONMENT` | 2 | The named deployment environment does not exist or has never been deployed. |
| `UNKNOWN_SERVICE` | 2 | The named service is not present in the deployment configuration. |
| `INTERACTION_CANCELLED` | 3 | A person was asked something and declined to answer. |
| `MISSING_ANSWER` | 3 | A run with nobody at the keyboard reached a question it has no answer for. |
| `ANSIBLE_FAILED` | 4 | An ansible playbook exited non-zero. |
| `DEPLOY_UNHEALTHY` | 4 | The deployment completed but the application did not come up healthy. |
| `DOCKER_FAILED` | 4 | A docker command exited non-zero. |
| `EDIT_REQUIRED` | 4 | A document failed validation, the model could not fix it, and nobody can be asked to. |
| `TERRAFORM_FAILED` | 4 | A terraform command exited non-zero. |
| `CLOUD_CREDENTIALS` | 5 | The cloud provider's credentials are missing, expired or unusable. |
| `CLOUD_PERMISSION` | 5 | The cloud credentials are valid but lack a permission the operation needs. |
| `LLM_GAVE_UP` | 6 | A model step could not produce a usable result within its configured limits. |
| `PENDING_ACTION` | 8 | The run is waiting on something only a person can do, such as creating a DNS record. |

## Result shapes

## AgentInstallResult

What `opsmith agent install` wrote.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `skill` | string | yes |  | The name the skill is installed under. |
| `version` | string | yes |  | The version of Opsmith the skill came from. |
| `installed` | AgentLocation[] | no |  | Every location the skill was written to. |
| `skipped` | AgentLocation[] | no |  | Locations that were not written, and why not. |
| `agents_md` | string | null | no | `None` | The AGENTS.md that was written, when --agents-md asked for it. |
| `claude_md` | string | null | no | `None` | The CLAUDE.md the import line was added to, when one was. |

## AgentStatusResult

Where the skill is installed, and whether it is current.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `skill` | string | yes |  | The name the skill installs under. |
| `package_version` | string | yes |  | The version of Opsmith running. |
| `skill_version` | string | yes |  | The version the packaged skill declares. |
| `locations` | AgentLocation[] | no |  | Every location Opsmith knows about, and its state. |
| `agents_md` | string | null | no | `None` | The AGENTS.md holding an Opsmith block, when there is one. |
| `claude_md` | string | null | no | `None` | The CLAUDE.md importing it, when there is one. |

## AgentUninstallResult

What `opsmith agent uninstall` removed.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `removed` | AgentLocation[] | no |  | Every location the skill was removed from. |
| `agents_md` | string | null | no | `None` | The AGENTS.md the managed block was stripped from, when there was one. |
| `claude_md` | string | null | no | `None` | The CLAUDE.md the import line was removed from, when there was one. |

## ConfigSchemaResult

The schema of the deployment configuration, in the rendering that was asked for.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `format` | string | yes |  | Which rendering this is: json or markdown. |
| `schema_document` | object | null | no | `None` | The JSON Schema itself, when the JSON rendering was asked for. |
| `markdown` | string | null | no | `None` | The markdown rendering, when that was asked for. |

## ConfigShowResult

The deployment configuration, as Opsmith reads it.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `config` | object | yes |  | The configuration, upgraded and normalised. |

## DestroyResult

What `opsmith destroy` tore down.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment that was destroyed. |
| `destroyed` | Resource[] | no |  | What was torn down, in the same shape the run that created it reported. An environment that was never deployed destroys nothing and says so with an empty list. |

## DockerfileValidateResult

What `opsmith dockerfile validate` found, over every service it checked.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `ok` | boolean | yes |  | Whether every service checked is usable as it stands. |
| `checks` | DockerfileCheck[] | no |  | One check per service, in configuration order. |

## EnvCreateResult

What `opsmith env create` created, and what it now needs from DNS.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment that was created. |
| `provider` | string | yes |  | The cloud provider it deploys to. |
| `region` | string | yes |  | The region it deploys into. |
| `strategy` | string | yes |  | The deployment strategy it uses. |
| `deployed` | boolean | no | `True` | Whether the environment was deployed, or only written to the config. |
| `resources` | Resource[] | no |  | Every piece of infrastructure the run created. |
| `registry_url` | string | null | no | `None` | The container registry that holds the images. It is among the resources too; it is named here because pushing an image is the first thing a driver does next. |
| `urls` | object | no |  | The url each routed service is reachable at, by slug. |
| `dns_records` | DnsRecord[] | no |  | The DNS records the deployment asked for. |

## EnvListResult

What `opsmith env list` found in the configuration.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environments` | EnvironmentSummary[] | no |  | Every environment the configuration declares. |

## EnvPlanResult

What `opsmith env plan` worked out that a run is going to ask for.

It is the exit-3 stop, reported all at once and before anything is created. Which questions
exist depends on the cloud provider and the strategy, so a plan that does not know those two
reports what it can and says the list is partial rather than claiming to be complete.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | null | no | `None` | The environment being planned, when one was named. |
| `provider` | string | null | no | `None` | The cloud provider the plan was made for, when one was chosen. |
| `strategy` | string | null | no | `None` | The deployment strategy the plan was made for, when one was chosen. |
| `complete` | boolean | yes |  | Whether this is the whole list. False when something still had to be chosen before the rest could be worked out, or when a provider or strategy declares no questions. |
| `answers_needed` | PlannedQuestion[] | no |  | Every answer the run will stop for, in the order asked. |
| `answers_known` | string[] | no |  | The keys that are already answered, by name. Values are not reported: some of them are secrets and this is printed. |
| `blocked_on` | string[] | no |  | The keys to answer first, before the rest of the list can be worked out. |
| `partial_reasons` | string[] | no |  | Why the list is not complete, in plain text. |
| `answers_file` | string | null | no | `None` | Where the answers skeleton was written, when one was asked for. |

## EnvStatusResult

What `opsmith env status` reads out of the environment's state file.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment being reported on. |
| `provider` | string | yes |  | The cloud provider it deploys to. |
| `region` | string | yes |  | The region it deploys into. |
| `strategy` | string | yes |  | The deployment strategy it uses. |
| `deployed` | boolean | yes |  | Whether it has been deployed. |
| `resources` | Resource[] | no |  | Every piece of infrastructure the environment holds. |
| `registry_url` | string | null | no | `None` | The container registry that holds the images. It is among the resources too; it is named here because pushing an image is the first thing a driver does next. |
| `services` | DeployedService[] | no |  | The services the last deploy or update put on it. |
| `urls` | object | no |  | The url each routed service is reachable at, by slug. |

## InitResult

What `opsmith init` created.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `app_name` | string | yes |  | The application name that was recorded. |
| `app_name_slug` | string | yes |  | The slug the name was reduced to. |
| `config_path` | string | yes |  | The configuration file that was written. |

## ReleaseResult

What `opsmith release` built and deployed.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment that was released to. |
| `images` | object | no |  | The image built for each service, by slug. |
| `services` | string[] | no |  | The services that were released. |
| `validated` | boolean | null | no | `None` | Whether the deployed stack came up healthy. None when nothing was deployed that could be validated. |
| `validation_reason` | string | null | no | `None` | What the validation found wrong, when it found something. |
| `urls` | object | no |  | The url each routed service is reachable at, by slug. |

## RunResult

What a command run on a deployed service did.

This is the one result that decides the process exit code: `opsmith run` returns what the
remote command returned, so a script driving it reads the same code it would have read had it
run the command itself.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment the command ran in. |
| `service` | string | yes |  | The service it ran on. |
| `target` | string | null | no | `None` | Where it ran, as the host or instance that answered. A strategy with more than one place to run a command picks one, and this is which one it picked. |
| `command` | string | yes |  | The command that was run. |
| `exit_code` | integer | yes |  | What the remote command exited with. |
| `stdout_tail` | string | no |  | The last lines the command wrote to stdout. |
| `stderr_tail` | string | no |  | The last lines the command wrote to stderr. |

## SetupResult

What `opsmith setup` detected and wrote.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `app_name` | string | yes |  | The application the configuration is for. |
| `services` | ServiceInfo[] | no |  | The services the run confirmed. |
| `dockerfiles` | string[] | no |  | The Dockerfiles written, one per service that needs one. |
| `infra_deps` | InfrastructureDependency[] | no |  | The infrastructure dependencies the run confirmed. |
| `config_path` | string | yes |  | The configuration file that was written. |

## UpdateResult

What `opsmith update` changed, or why it changed nothing.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `environment` | string | yes |  | The environment that was updated. |
| `applied` | boolean | yes |  | Whether the update reached the environment. |
| `reason` | string | null | no | `None` | Why nothing was applied, when nothing was. |
| `changes` | object | no |  | What the configuration changed, by kind: services and infra, added, removed and modified. |
| `images` | object | no |  | The image rebuilt for each service, by slug. |
| `urls` | object | no |  | The url each routed service is reachable at, by slug. |

## ValidateResult

What `opsmith config validate` found.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `notices` | Notice[] | no |  | What the run told the user while it worked. |
| `next_steps` | string[] | no |  | What the user or the driver should do next. |
| `ok` | boolean | yes |  | Whether the configuration is usable. |
| `errors` | ConfigIssue[] | no |  | Problems that stop the configuration being used. |
| `warnings` | ConfigIssue[] | no |  | Problems that are suspicious but not fatal. |

## AgentLocation

One place a harness reads skills from, and what Opsmith found there.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `target` | string | yes |  | The harness this location belongs to. |
| `label` | string | yes |  | What that harness is called, for a person reading. |
| `scope` | string | yes |  | Whether this is the project or the user location. |
| `path` | string | yes |  | The directory the skill is installed into. |
| `state` | AgentLocationState | yes |  | What is there now. |
| `installed_version` | string | null | no | `None` | The version of the skill installed there, when one is. |
| `verified` | boolean | yes |  | Whether this path has been confirmed against the harness itself. An unverified path is Opsmith's best reading of a convention that is still moving. |

## AgentLocationState

What `opsmith agent status` found at one skill location.

One of: `installed`, `stale`, `missing`, `foreign`, `not_applicable`

## ChoiceOption

One option of a question, in the form an answer can name it by.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `label` | string | yes |  | What a person would see for this option. |
| `value` | string | yes |  | What to supply to choose it. |

## ConfigIssue

One problem found in a configuration, at one place in it.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `path` | string | yes |  | Where the problem is, as a dotted path such as services.0.service_port. |
| `message` | string | yes |  | What is wrong there. |

## DependencyTypeEnum

Enum for the different types of infrastructure dependencies.

One of: `DATABASE`, `CACHE`, `MESSAGE_QUEUE`, `SEARCH_ENGINE`

## DeployedService

One service an environment is currently running.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name_slug` | string | yes |  | The slug the service is addressed by. |
| `image` | string | null | no | `None` | The image it is running, when the strategy records which one. |
| `url` | string | null | no | `None` | Where it is reachable, when it is routed. |

## DnsRecord

One DNS record a deployment needs somebody to create.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `type` | string | yes |  | The record type, such as A or CNAME. |
| `name` | string | yes |  | The name the record is created under. |
| `value` | string | yes |  | What the record points at. |

## DockerfileCheck

What building and running one service's Dockerfile did.

`ok` is the verdict to act on and is not the same as `build_ok and run_ok`: when docker
fails, the model is asked whether the failure is the Dockerfile's fault, and a container that
exits because the database it wants does not exist yet is not. `dockerfile_at_fault` is what
keeps `build_ok: false, ok: true` legible.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `service` | string | yes |  | The slug of the service that was checked. |
| `dockerfile` | string | yes |  | The Dockerfile that was checked, relative to the repo. |
| `ok` | boolean | yes |  | Whether the Dockerfile is usable as it stands. |
| `build_ok` | boolean | yes |  | Whether docker build succeeded. |
| `run_ok` | boolean | null | no | `None` | Whether the container ran. Unset when the build failed, because the run never happened. |
| `run_timed_out` | boolean | no | `False` | Whether the container was still up when the watch ended, which counts as healthy. |
| `dockerfile_at_fault` | boolean | null | no | `None` | Whether the model judged the failure fixable in the Dockerfile. Unset when docker succeeded and nothing needed judging. |
| `explanation` | string | null | no | `None` | What the model said went wrong, when something did. |
| `build_tail` | string | no |  | The last lines of the build output. |
| `run_tail` | string | no |  | The last lines of the run output. |

## EnvVarConfig

Describes an environment variable configuration for a service.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `key` | string | yes |  | The name of the environment variable. |
| `is_secret` | boolean | yes |  | Whether the environment variable should be treated as a secret. |
| `default_value` | string | null | no | `None` | The default value of the environment variable, if present in the code. |

## EnvironmentSummary

One line of `opsmith env list`.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes |  | The environment's name. |
| `provider` | string | yes |  | The cloud provider it deploys to. |
| `region` | string | yes |  | The region it deploys into. |
| `strategy` | string | yes |  | The deployment strategy it uses. |
| `deployed` | boolean | yes |  | Whether it has been deployed, which is whether it has a state file. |

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

## Notice

One thing a run told the user through :meth:`Interaction.notify`.

It lives here rather than in `core/results.py` because `notify` is what produces it, and
because results imports `opsmith.types`, which reaches `cloud_providers` and back to this
module - an import this way round closes that circle, the other way round does not.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `message` | string | yes |  | What the run said, in plain text. |
| `details` | any | null | no | `None` | Machine-readable context the notice carried, when it carried any. |

## PlannedQuestion

One answer a run is going to need, described for somebody who has not run it yet.

It carries what the exit-3 stop carries, so a driver reads the same fields whether it
discovered the question by planning or by colliding with it.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `key` | string | yes |  | The stable key this answer is addressed by. |
| `message` | string | yes |  | The question, in plain text. |
| `primitive` | string | yes |  | Which primitive asks it: ask, select or confirm. |
| `asked_by` | string | no |  | What asks it: the command, the provider or the strategy. |
| `required` | boolean | yes |  | Whether the run will stop here. False when the question has a default that this run's options would take. |
| `secret` | boolean | no | `False` | Whether the answer must not be put on a command line or in a file. |
| `default` | string | null | no | `None` | What it falls back to, when it has one. |
| `choices` | ChoiceOption[] | null | no | `None` | The options to choose between. Null means they are not known here - a listing that needs credentials, or an earlier answer - not that there are none. |
| `env_var` | string | yes |  | The environment variable that answers this key. |

## Resource

One piece of infrastructure a strategy made, and enough to address it.

Strategies report what they created as a list of these rather than as named fields, because
a named `public_ip` is a claim that there is exactly one machine and that it has an IP.
A strategy that raises several machines reports several of these; one that deploys to a
cluster or to a serverless platform reports what it actually made and leaves the fields that
do not apply unset.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `kind` | string | yes |  | What sort of thing this is: a ResourceKind value, or a strategy's own word for something Opsmith does not name. |
| `id` | string | yes |  | How the provider addresses it, such as an instance ID. |
| `name` | string | null | no | `None` | What it is called, when it has a name separate from its ID. |
| `region` | string | null | no | `None` | Where it was created. |
| `address` | string | null | no | `None` | Where it is reached, as an IP or a hostname, when it is reachable. |
| `size` | string | null | no | `None` | How big it is, as the provider names it: an instance type or a tier. |
| `details` | object | no |  | Whatever else this kind carries that the fields above do not name. |

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
