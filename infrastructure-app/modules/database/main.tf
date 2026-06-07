# 1. Generation of the password in memory
resource "random_password" "db_password" {
  length           = 16
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# 2. Sending the generated password to AWS SSM Parameter Store
resource "aws_ssm_parameter" "db_password" {
  name        = "/${var.environment}/database/${var.db_identifier}/password"
  description = "Master password for RDS instance ${var.db_identifier}"
  type        = "SecureString"
  value       = random_password.db_password.result
}

# 3. Provisioning the database server
resource "aws_db_instance" "postgresql" {
  identifier           = var.db_identifier
  allocated_storage    = 20
  engine               = "postgres"
  engine_version       = "15"
  instance_class       = "db.t3.micro"
  username             = "dbadmin"
  password             = random_password.db_password.result
  

  # system_parameters
  skip_final_snapshot  = true
  publicly_accessible  = false
}

resource "aws_iam_role_policy" "rds_password_access" {
  name   = "bot-rds-password-access"
  role   = var.iam_role_id # We use the IAM role created in the network module, which is attached to our EC2 instances (application servers)

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = [
          "ssm:GetParameter" # Exact match: Right to read the value
        ]
        Resource = aws_ssm_parameter.db_password.arn # Reference to the ARN of the parameter with the password from the database module
      },
      # Technical addition: Since we used SecureString (encryption),
      # the server needs the mathematical right to decrypt this string with the standard AWS key.
      {
        Effect   = "Allow"
        Action   = [
          "kms:Decrypt" 
        ]
        Resource = "arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      }
    ]
  })
}

# Get the current region
data "aws_region" "current" {}

# Get the current account number (Caller Identity)
data "aws_caller_identity" "current" {}