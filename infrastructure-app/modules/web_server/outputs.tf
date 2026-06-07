output "server_public_ip" {
  description = "Public IP address of the EC2 instance"
  value       = aws_instance.my_server.public_ip
}

output "private_ip" {
  description = "Private IP address of the EC2 instance"
  value = aws_instance.my_server.private_ip
}

output "instance_id" {
  description = "Instance ID of the EC2 instance"
  value = aws_instance.my_server.id
}