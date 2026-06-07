terraform {
  required_version = ">=1.5.7"
  required_providers {
    aws = { source = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  }

provider "aws" {
  region = "eu-central-1"
  default_tags {
    tags = {
      Project   = "Infrastructure-Sceleton"
      ManagedBy = "Terraform"
    Environment = "Production" }
  }
}

resource "aws_s3_bucket" "my_bucket" {
  bucket = "algev-1990-tfstatelocker"
  force_destroy = true
}

resource "aws_dynamodb_table" "my_ddb_table" {
  name         = "terraform-state-lock"
  hash_key     = "LockID"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "LockID"
    type = "S"
  }
}
