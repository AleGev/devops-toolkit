# Внутри infrastructure-network/variables.tf
variable "public_subnets_cidr" {
  description = "Список адресов для публичных подсетей"
  type        = list(string)  # Мы говорим, что ждем список строк
  default = ["10.0.1.0/24", "10.0.3.0/24"]
}

variable "private_subnets_cidr" {
  description = "Список адресов для приватных подсетей"
  type        = list(string)
  default = ["10.0.2.0/24", "10.0.4.0/24"]
}

variable "azs" {
  description = "Список зон доступности"
  type        = list(string)
  default = ["eu-central-1a", "eu-central-1b"]
}