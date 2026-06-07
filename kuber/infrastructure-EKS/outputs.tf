output "eks_sg_id" {
  description = "ID of the EKS security group"
  value       = module.eks.cluster_security_group_id
}

output "eks_cluster_endpoint" {
  description = "Endpoint for the EKS cluster API"
  value       = module.eks.cluster_endpoint
}

output "irsa_iam_role_arn" {
  description = "ARN of IAM role"
  value       = module.irsa.arn # returning a list of ARNs for all IRSA roles, if there are multiple
}

output "irsa_app_iam_role_arn" {
  description = "ARN of IAM role for app"
  value       = module.irsa_app.arn # returning the ARN of the role for the application
}