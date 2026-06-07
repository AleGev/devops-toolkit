terraform {
  required_version = ">=1.5.7"
  required_providers {
    aws = { source = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket         = "algev-1990-tfstatelocker"
    key            = "terraform.tfstate"
    region         = "eu-central-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
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