# 1. Fetching network parameters from existing infrastructure
data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "algev-1990-tfstatelocker"
    key    = "network/terraform.tfstate" # Specify the exact state key for your network
    region = "eu-central-1"
  }
}

# 2. Forming network routing rules (Security Group)
resource "aws_security_group" "redis" {

  name        = "elasticache-redis-sg"
  description = "Security Group for Redis Cluster"
  vpc_id      = data.terraform_remote_state.network.outputs.vpc_id

  ingress {
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    
    cidr_blocks = data.terraform_remote_state.network.outputs.private_subnet_cidr_blocks # Only allow access from private subnets
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# 3. Binding the database to AWS subnets
resource "aws_elasticache_subnet_group" "redis_subnets" {
  name       = "redis-subnet-group"
  subnet_ids = data.terraform_remote_state.network.outputs.private_subnet_ids
}

# 4. Allocating a computational node for the database
resource "aws_elasticache_cluster" "redis_node" {
  cluster_id           = "my-bot-redis"
  engine               = "redis"
  node_type            = "cache.t3.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  engine_version       = "7.0"
  port                 = 6379
  
  subnet_group_name    = aws_elasticache_subnet_group.redis_subnets.name
  security_group_ids   = [aws_security_group.redis.id]
}