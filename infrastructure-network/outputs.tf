output "nat_instance_public_ip" {
  description = "Public IP address of the NAT instance"
  value       = aws_eip.nat_eip.public_ip
}

output "vpc_id" {
  description = "ID of the global VPC container"
  value       = aws_vpc.custom_vpc.id
}

output "vpc_cidr_block" {
  description = "CIDR block of the VPC"
  value       = aws_vpc.custom_vpc.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = aws_subnet.public_subnet[*].id 
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = aws_subnet.private_subnet[*].id
}
output "private_subnet_cidr_blocks" {
  description = "CIDR blocks of the private subnets"
  value       = aws_subnet.private_subnet[*].cidr_block
}

output "iam_instance_profile_name" {
  description = "Name of the IAM instance profile for SSM Agent"
  value       = aws_iam_instance_profile.profile.name
}

output "aws_iam_role_id" {
  value = aws_iam_role.role.id
}