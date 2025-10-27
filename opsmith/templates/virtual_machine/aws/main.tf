provider "aws" {
  region = var.region
}

data "aws_ssm_parameter" "ecs_optimized_ami" {
  name = (
    var.instance_arch == "arm64" ?
    "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended" :
    "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended"
  )
}


# Local values
locals {
  ami_id = jsondecode(data.aws_ssm_parameter.ecs_optimized_ami.value)["image_id"]

  common_tags = {
    Project     = var.app_name
    ManagedBy   = "Opsmith"
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-vpc"
  })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-public-subnet"
    Type = "Public"
  })
}

resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.main.id
  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-igw"
  })
}

resource "aws_route_table" "rt" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-rt"
  })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.rt.id
}

# VPC Flow Logs
resource "aws_cloudwatch_log_group" "vpc_flow_log" {
  name              = "/aws/vpc/flowlogs/${var.app_name}-${var.environment}"
  retention_in_days = 7

  tags = local.common_tags
}

resource "aws_iam_role" "flow_log_role" {
  name = "${var.app_name}-${var.environment}-flow-log-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "flow_log_policy" {
  name = "${var.app_name}-${var.environment}-flow-log-policy"
  role = aws_iam_role.flow_log_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

resource "aws_flow_log" "vpc_flow_log" {
  iam_role_arn    = aws_iam_role.flow_log_role.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_log.arn
  traffic_type    = "ALL"
  vpc_id          = aws_vpc.main.id

  tags = local.common_tags
}

# S3 Bucket for SSM Session Logs
resource "aws_s3_bucket" "ssm_session_logs" {
  bucket = "${var.app_name}-${var.environment}-ssm-logs"

  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-ssm-session-logs"
  })
}

resource "aws_s3_bucket_public_access_block" "ssm_session_logs_access" {
  bucket = aws_s3_bucket.ssm_session_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ssm_session_logs_encryption" {
  bucket = aws_s3_bucket.ssm_session_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# SSM Session Manager Preferences
resource "aws_ssm_document" "ssm_session_preferences" {
  name          = "${var.app_name}-${var.environment}-ssm-session-prefs"
  document_type = "Session"
  content = jsonencode({
    schemaVersion = "1.0",
    description   = "SSM Session Manager Preferences for ${var.app_name}-${var.environment}",
    sessionType   = "Standard_Stream",
    inputs = {
      s3KeyPrefix         = "ssm-sessions",
      s3EncryptionEnabled = true,
      runAsEnabled        = true,
      runAsDefaultUser    = "ec2-user"
    }
  })
  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-ssm-prefs"
  })
}


# Security Groups
resource "aws_security_group" "instance_sg" {
  name        = "${var.app_name}-${var.environment}-sg"
  description = "Security group for ${var.app_name}-${var.environment} monolithic instance"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags,{
    Name = "${var.app_name}-${var.environment}-sg"
  })
}

# IAM Role and Instance Profile
resource "aws_iam_role" "ec2_role" {
  name = "${var.app_name}-${var.environment}-ec2-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action    = "sts:AssumeRole",
        Effect    = "Allow",
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecr_access" {
  role       = aws_iam_role.ec2_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "ssm_managed_instance" {
  role       = aws_iam_role.ec2_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.ec2_role.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_instance_profile" "ec2_instance_profile" {
  name = "${var.app_name}-${var.environment}-ec2-instance-profile"
  role = aws_iam_role.ec2_role.name
}

resource "aws_instance" "app_server" {
  ami           = local.ami_id
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public.id
  iam_instance_profile = aws_iam_instance_profile.ec2_instance_profile.name
  vpc_security_group_ids = [aws_security_group.instance_sg.id]

  depends_on = [
    aws_iam_role_policy_attachment.ecr_access,
    aws_iam_role_policy_attachment.ssm_managed_instance,
    aws_iam_role_policy_attachment.cloudwatch_agent,
  ]

  root_block_device {
    volume_type           = "gp3"
    encrypted            = true
    delete_on_termination = true

    tags = merge(local.common_tags, {
      Name = "${var.app_name}-${var.environment}-root-volume"
    })
  }

  tags = merge(local.common_tags, {
    Name = "${var.app_name}-${var.environment}-monolithic-server"
  })
}

