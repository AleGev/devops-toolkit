variable "app_token" {
  description = "The token for the application"
  type        = string 
  sensitive   = true # Hides the value from Terraform logs
}

variable "redis_host" {
  description = "Endpoint for the Redis cluster"
  type        = string
}