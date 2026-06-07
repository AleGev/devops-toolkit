terraform {
  required_version = ">=1.5.7"
  required_providers {
    aws = { 
      source  = "hashicorp/aws"
      version = "~> 6.28"
    }
  }
  backend "s3" {
    bucket         = "algev-1990-tfstatelocker"
    key            = "eks/terraform.tfstate" # Strictly allocated key for EKS
    region         = "eu-central-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = "eu-central-1"
  default_tags {
    tags = {
      Project     = "Infrastructure-EKS"
      ManagedBy   = "Terraform"
      Environment = "Production"
    }
  }
}