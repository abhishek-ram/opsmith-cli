# Opsmith: An AI devops engineer in your terminal

Opsmith is a command-line tool that acts as an AI-powered DevOps assistant. It's designed to streamline the process of deploying your applications to the cloud, from analyzing your codebase to provisioning infrastructure and deploying your services.

Opsmith helps you with the following tasks:

- **Codebase Analysis**: It scans your repository to automatically detect services, programming languages, frameworks, and infrastructure dependencies (like databases or caches).
- **Configuration Generation**: Based on its analysis, Opsmith generates necessary deployment artifacts.
- **Infrastructure Provisioning**: It uses tools like Terraform and Ansible to provision and configure required cloud resources on supported providers (e.g., AWS, GCP).
- **Deployment**: It handles the deployment of your application using various strategies, such as a monolithic deployment on a single virtual machine for hobby projects.

The primary goal of Opsmith is to make cloud deployments accessible to all developers, regardless of their DevOps expertise. It achieves this by automating complex tasks through an interactive setup process, allowing you to focus on writing code. Opsmith is also designed to prevent cloud provider lock-in, which helps control long-term costs. The generated configurations are standard and maintainable, making it easy to hand over the deployment to an in-house DevOps team.

## Table of Contents

- [Getting Started](#getting-started)
  - [Installation](#installation)
  - [Deployment Workflow](#deployment-workflow)
- [User Guide](#user-guide)
  - [LLMs](#llms)
    - [Supported Models](#supported-models)
  - [Cloud Providers](#cloud-providers)
    - [AWS](#aws-amazon-web-services)
    - [GCP](#gcp-google-cloud-platform)
  - [Deployment Strategies](#deployment-strategies)
    - [Monolithic](#monolithic)
  - [Deployments Directory](#deployments-directory)
  - [Extending Opsmith](#extending-opsmith)
    - [Adding a Cloud Provider](#adding-a-cloud-provider)
    - [Adding a Deployment Strategy](#adding-a-deployment-strategy)
- [Contributing](#contributing)

## Getting Started

### Installation

1.  **Prerequisites**: Opsmith requires `Docker` and `Terraform` to be installed and available in your system's `PATH`.

    -   **macOS (with [Homebrew](https://brew.sh/))**:
        ```shell
        brew install --cask docker
        brew install terraform
        ```
        After installation, make sure you start Docker Desktop.

    -   **Windows (with [Chocolatey](https://chocolatey.org/))**:
        ```shell
        choco install docker-desktop terraform
        ```
        After installation, make sure you start Docker Desktop.

    -   **Linux (Debian/Ubuntu)**:
        Please follow the official installation guides for [Docker](https://docs.docker.com/engine/install/ubuntu/) and [Terraform](https://developer.hashicorp.com/terraform/install).

2.  **Install Opsmith**:
    Once the prerequisites are installed, you can install Opsmith using `pip`:
    ```shell
    pip install opsmith-cli
    ```
    On macOS, you might need to use `pip3` if `pip` doesn't work.

### Deployment Workflow

Deploying your application with Opsmith follows a straightforward workflow:

1.  **Setup Your Project**

    Navigate to your project's root directory, which should be a Git repository, and run the `setup` command. This command initializes your deployment configuration by analyzing your codebase to detect services and infrastructure requirements.

    ```shell
    opsmith --model <your-llm-provider:model-name> --api-key <your-api-key> setup
    ```
    <p >
      <img width="600" src="https://raw.githubusercontent.com/abhishek-ram/opsmith-cli/main/resources/setup_demo.svg" alt="Opsmith Setup">
    </p>
    
2.  **Deploy Your Application**

    After setting up the configuration, deploy your application using the `deploy` command:

    ```shell
    opsmith --model <your-llm-provider:model-name> --api-key <your-api-key> deploy
    ```

    <p >
      <img width="600" src="https://raw.githubusercontent.com/abhishek-ram/opsmith-cli/main/resources/deploy_demo.svg" alt="Opsmith Setup">
    </p>

3.  **Manage Your Deployments**

    To manage an existing environment, run the `deploy` command again. You can select an environment and perform the following actions:
    -   `release`: Deploy a new version of your application.
    -   `run`: Execute a command on a specific service within your environment (e.g., run database migrations).
    -   `delete`: Tear down all the infrastructure and delete the environment.

## User Guide

### LLMs

Opsmith leverages Large Language Models (LLMs) to analyze your codebase, generate configurations, and make decisions about your infrastructure. To use Opsmith, you must provide an LLM model and a corresponding API key from the model's provider.

#### Supported Models

Opsmith supports a variety of models from different providers. Here is a list of the currently supported models:

- **Google** (Recommended):
  - `google-gla:gemini-2.5-pro`
- **OpenAI**:
  - `openai:gpt-4.1`
  - `openai:gpt-o3`
- **Anthropic**:
  - `anthropic:claude-3-7-sonnet-20250219`
  - `anthropic:claude-sonnet-4-20250514`

#### Usage

To specify which model to use, pass the `--model` option with the full model name, and provide your API key with the `--api-key` option.

**Example with OpenAI:**
```shell
opsmith --model openai:gpt-4.1 --api-key YOUR_OPENAI_API_KEY COMMAND
```

**Example with Anthropic:**
```shell
opsmith --model anthropic:claude-3-7-sonnet-20250219 --api-key YOUR_ANTHROPIC_API_KEY COMMAND
```

**Example with Google:**
```shell
opsmith --model google-gla:gemini-2.5-pro --api-key YOUR_GEMINI_API_KEY COMMAND
```

### Cloud Providers

Opsmith uses your cloud provider's command-line tools to authenticate and manage resources. Before using Opsmith, you need to configure the credentials for your chosen cloud provider.

#### AWS (Amazon Web Services)

Opsmith uses the official AWS CLI to interact with your account.

1.  **Install the AWS CLI**: Follow the [official installation guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) for your operating system.

2.  **Configure Credentials**: Once installed, configure the CLI with your AWS credentials by running:
    ```shell
    aws configure
    ```
    You will be prompted to enter your `AWS Access Key ID`, `AWS Secret Access Key`, default region, and default output format. This will store your credentials in the `~/.aws/credentials` file, which Opsmith will use automatically.

#### GCP (Google Cloud Platform)

Opsmith uses the Google Cloud CLI to authenticate.

1.  **Install the gcloud CLI**: Follow the [official installation guide](https://cloud.google.com/sdk/docs/install) for your operating system.

2.  **Authenticate with Application Default Credentials (ADC)**: Run the following command to log in and create your ADC file:
    ```shell
    gcloud auth application-default login
    ```
    This command will open a browser window for you to log in to your Google account and authorize access. Once completed, your credentials will be stored locally, and Opsmith will use them to authenticate.

### Deployment Strategies

Deployment strategies in Opsmith define the architecture and approach for deploying your application. When you create a new environment, you will be prompted to select a strategy that best fits your project's needs. Each strategy automates the provisioning of specific infrastructure and handles the deployment process accordingly.

#### Monolithic

The **Monolithic** strategy is designed for simplicity and is ideal for hobby projects, experiments, or small-scale applications. It deploys your entire application to a single virtual machine (VM).

Key features of the Monolithic strategy include:

- **Single Virtual Machine**: Provisions one VM to host all backend services and infrastructure dependencies.
- **Containerization**: Backend and full-stack services are containerized using Docker and managed with `docker-compose`. This isolates services and simplifies dependency management.
- **Frontend Deployment**: Frontend services are built and deployed to a cloud storage bucket (like AWS S3 or GCS) and served through a Content Delivery Network (CDN) for optimal performance.

This strategy is a great starting point for getting your application running in the cloud quickly with minimal complexity.

### Deployments Directory

When you run `opsmith`, it creates a `.opsmith` directory in the root of your project. This directory stores all the configurations, generated artifacts, and state files required to manage your deployments. This directory is intended to be committed to your git repository so that your deployment configurations are versioned. Sensitive files like Terraform state are automatically ignored.

Here is an overview of what you can find inside the `.opsmith` directory:

-   `deployments.yml`: The main configuration file for your application. It contains the list of services, infrastructure dependencies, cloud provider details, and environment configurations.
-   `docker/`: Contains the generated `Dockerfile`s for each of your services, organized into subdirectories by service name.
-   `environments/`: This directory holds the state and configuration for each of your deployment environments (e.g., `dev`, `staging`).
    -   `<environment-name>/`: A directory for each environment, containing Terraform state for provisioned infrastructure and other environment-specific files.
    -   `global/`: Contains configurations that are shared across environments within a specific region, such as container registries.

### Extending Opsmith

Opsmith has been designed with extensibility in mind, allowing you to add your own cloud providers and deployment strategies. This is achieved through Python's entry points mechanism, which enables other packages to plug into Opsmith seamlessly.

#### Adding a Cloud Provider

To add a new cloud provider, you need to:

1.  Create a Python class that inherits from `opsmith.cloud_providers.base.BaseCloudProvider` and implements all its abstract methods (`name`, `description`, `get_detail_model`, `get_account_details`, `get_instance_types`, `get_regions`).
2.  Package your new provider class.
3.  In your package's `pyproject.toml`, add an entry point under the `[project.entry-points."opsmith.cloud_providers"]` group.

Example `pyproject.toml` entry:

```toml
[project.entry-points."opsmith.cloud_providers"]
my-provider = "my_opsmith_plugin.providers:MyCloudProvider"
```

Once your package is installed in the same environment as `opsmith-cli`, Opsmith will automatically discover and register your new provider.

#### Adding a Deployment Strategy

Similarly, you can add a new deployment strategy by:

1.  Creating a Python class that inherits from `opsmith.deployment_strategies.base.BaseDeploymentStrategy` and implements its abstract methods (`name`, `description`, `deploy`, `release`, `destroy`, `run`).
2.  Packaging your new strategy class.
3.  In your package's `pyproject.toml`, add an entry point under the `[project.entry-points."opsmith.deployment_strategies"]` group.

Example `pyproject.toml` entry:

```toml
[project.entry-points."opsmith.deployment_strategies"]
my-strategy = "my_opsmith_plugin.strategies:MyStrategy"
```

After installation, your custom deployment strategy will be available for selection when creating a new environment.

## Contributing

We welcome contributions to Opsmith! If you're interested in helping improve the tool, here are a few ways to get started:

-   **Reporting Bugs**: If you encounter a bug, please open an issue on our GitHub repository. Include as much detail as possible, such as your operating system, the command you ran, and the full error message.
-   **Suggesting Enhancements**: Have an idea for a new feature or an improvement to an existing one? We'd love to hear it. Open an issue to start a discussion.
-   **Submitting Pull Requests**: If you'd like to contribute code, please fork the repository and submit a pull request. For major changes, it's best to discuss your idea in an issue first.

When contributing, please follow our [engineering conventions](./CLAUDE.md) and ensure your code is formatted with `black`.
