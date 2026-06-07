variable "server_name" {
    type = string
    description = "Server name for tags"  
}
variable "instance_type" {
    type = string
    description = "Server size"  
}

variable "allowed_ingress_cidr" {
    type = list(string)
    description = "list of allowed ip adresses"
}

variable "public_key_path" {
  type = string
  description = "Pass to publick key"
  sensitive = true
}

variable "vpc_id" {
  description = "VPC ID for Security Group"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for EC2 instance"
  type        = string
}

variable "iam_instance_profile" {
  description = "IAM instance profile for EC2 instance"
  type        = string
}

variable "iam_role_id" {
  description = "IAM role id"
  type        = string
  
}


#variable "user_data_path" {
   # type = string
    #description = "pass to bash script setup.sh"
  
#}

#variable "user_data_content" {
  #type        = string
  #description = "Содержимое файла инициализации cloud-config.yaml"
#}