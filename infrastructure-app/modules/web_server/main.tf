resource "aws_security_group" "web_traffic" {
    name = "${var.server_name}-sg"
    description = "Port settings"
    vpc_id = var.vpc_id

    ingress {
      from_port = 80
      to_port = 80
      protocol = "tcp"
      cidr_blocks = var.allowed_ingress_cidr
    }

    ingress {
      from_port = 22
      to_port = 22
      protocol = "tcp"
      cidr_blocks = var.allowed_ingress_cidr
    }
    
    egress {
      from_port = 0
      to_port = 0
      protocol = "-1"
      cidr_blocks = ["0.0.0.0/0"]
    }
}


data "aws_ami" "amazon_linux" {
    most_recent = true
    owners = ["amazon"]

    filter {
      name = "name"
      values = ["al2023-ami-*-x86_64"]
    }
}

resource "aws_key_pair" "deployer" {
key_name = "${var.server_name}-key"
public_key = var.public_key_path
}

resource "aws_instance" "my_server" {
    ami = data.aws_ami.amazon_linux.id
    instance_type = var.instance_type
    key_name = aws_key_pair.deployer.key_name
    #user_data = var.user_data_content
    vpc_security_group_ids = [aws_security_group.web_traffic.id]
    subnet_id = var.subnet_id
    iam_instance_profile = var.iam_instance_profile
    
# 1. STRICT INSTRUCTION: Destroy and recreate the server when the script changes
    user_data_replace_on_change = true

    user_data = <<-EOF
                # --- НОВЫЙ БЛОК: ПРИНУДИТЕЛЬНЫЙ СТАРТ SSM ---
                systemctl enable amazon-ssm-agent
                systemctl start amazon-ssm-agent
                EOF



metadata_options {
  http_endpoint = "enabled"
  http_tokens = "required"
  http_put_response_hop_limit = 1
}

root_block_device {
  volume_size = 30
  volume_type = "gp3"
  encrypted = true
}
tags = { Name = var.server_name }

lifecycle { prevent_destroy = false }

}

resource "aws_cloudwatch_log_group" "app_logs" {
  name = "/aws/ec2/${var.server_name}/logs"

  # Financial safety: AWS stores logs forever and charges for it.
  # We instruct the system to automatically delete logs older than 7 days.
  retention_in_days = 7 
}

resource "aws_iam_role_policy" "cloudwatch_policy" {
  name   = "${var.server_name}-cloudwatch-logs-policy"
  role   = var.iam_role_id 

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "logs:CreateLogStream", # rights to create a new log stream (a file in which logs will be written)
          "logs:PutLogEvents"     # rights to write text to this file
        ]
        Effect   = "Allow"
        # We grant the right to write ONLY to our specific folder (Zero Trust)
        # And to all files within it (the * symbol is a mathematical symbol for 'anything after the colon').
        Resource = "${aws_cloudwatch_log_group.app_logs.arn}:*"
      }
    ]
  })
}