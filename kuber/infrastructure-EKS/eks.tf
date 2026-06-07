data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "algev-1990-tfstatelocker"
    key    = "network/terraform.tfstate" # Укажи точный ключ состояния твоей сети
    region = "eu-central-1"
  }
}


module "irsa" {
  source = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  name = "load-balancer-controller"
  version = "~> 6.6.0" 

 attach_load_balancer_controller_policy = true
 
  oidc_providers = {
    lbc = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"] # Указываем namespace и имя service account, который будет использоваться контроллером нагрузки 
    } 
  }

  tags = {
    Terraform   = "true"  
    Environment = "dev"
  }
}

module "irsa_app" {
  source = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  name = "ssm-app-role"
  version = "~> 6.6.0" 
  
  policies = {
    policy = aws_iam_policy.bot_ssm_policy.arn
  }
  
  oidc_providers = {
    app = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["default:app-sa"] # Указываем namespace и имя service account, который будет использоваться контроллером нагрузки 
    } 
  }

  tags = {
    Terraform   = "true"  
    Environment = "dev"
  }
}

resource "aws_iam_policy" "app_ssm_policy" {
  name   = "app-ssm-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = [
          "ssm:GetParameter"
        ]
        Resource = [aws_ssm_parameter.app_token.arn, aws_ssm_parameter.redis_host.arn],# Ссылка на ARN параметра с секретами бота
      },
      # Additional requirement: since SecureString encryption is used,
      # the server needs permission to decrypt the value using the AWS managed KMS key.
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

resource "aws_ssm_parameter" "app_token" {
  name  = "/production/app/token"
  type  = "SecureString"
  value = var.app_token
}

resource "aws_ssm_parameter" "redis_host" {
  name  = "/production/redis/host"
  type  = "String"
  value = var.redis_host
}


# get current region
data "aws_region" "current" {}

# get current account ID
data "aws_caller_identity" "current" {}


module "eks" {
  source             = "terraform-aws-modules/eks/aws"
  version            = "~> 21.20.0"
  
  name    = "my-cluster"
  kubernetes_version = "1.31"

  addons = {
    coredns                = {}
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy             = {}
    vpc-cni                = {
      before_compute = true
      configuration_values = jsonencode({
        env = {
          ENABLE_PREFIX_DELEGATION = "true" # Enable prefix delegation so the CNI can allocate IP addresses from the subnet for pods instead of using the cluster IP range.
          WARM_PREFIX_TARGET       = "1"   # Keep one warm prefix always available and ready for use.. 
        }
      })
    }
  }


  # 1. Network attachment
vpc_id = data.terraform_remote_state.network.outputs.vpc_id

# The control plane is attached to all subnets
subnet_ids = concat(
  data.terraform_remote_state.network.outputs.private_subnet_ids,
  data.terraform_remote_state.network.outputs.public_subnet_ids
)

# 2. API access configuration
endpoint_public_access  = true # Allows managing the cluster from a local machine
endpoint_private_access = true # Nodes communicate with the API internally within AWS without exposing traffic to the internet

# Allow the Terraform executor to create resources in the cluster
enable_cluster_creator_admin_permissions = true

# 3. Worker node group configuration
  eks_managed_node_groups = {
    core_nodes = {
     # Starting on 1.30, AL2023 is the default AMI type for EKS managed node groups
      ami_type       = "AL2023_x86_64_STANDARD"
      # Worker nodes run ONLY in private subnets!
      subnet_ids = data.terraform_remote_state.network.outputs.private_subnet_ids
      instance_types = ["m5.xlarge"] 
      
      min_size     = 1
      max_size     = 1
      desired_size = 1

      labels = {
        role = "core_nodes"
      }
    }

    db_nodes = {
      # Starting on 1.30, AL2023 is the default AMI type for EKS managed node groups
      ami_type       = "AL2023_x86_64_STANDARD"
      # Worker nodes for the database run ONLY in private subnets!
      subnet_ids = data.terraform_remote_state.network.outputs.private_subnet_ids
      instance_types = ["m5.xlarge"] 
      taints = {
        dedicated = {
          key    = "role"
          value  = "db_nodes"
          effect = "NO_SCHEDULE"
        }
      }

      min_size     = 1
      max_size     = 1
      desired_size = 1
      
      labels = {
        role = "db_nodes"
      }
    }
  }
}


data "terraform_remote_state" "redis" {
  backend = "s3"
  config = {
    bucket = "algev-1990-tfstatelocker"
    key    = "storage/terraform.tfstate" # Specify the exact key for your database's state
    region = "eu-central-1"
  }
}

resource "aws_security_group_rule" "allow_eks_to_redis" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  # 1. Which "door" are we opening? (Where is the port opened?) -> On the Redis database security group.
  security_group_id        = data.terraform_remote_state.redis.outputs.redis_security_group_id
  
  ## 2. Who is allowed in? -> Kubernetes worker nodes
  # Important: we use node_security_group_id (nodes), not cluster_security_group_id (Kubernetes API)
  source_security_group_id = module.eks.node_security_group_id
}


