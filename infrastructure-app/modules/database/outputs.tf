output "db_endpoint" {
  description = "Database endpoint"
  value       = aws_db_instance.postgresql.endpoint
}

output "db_identifier" {
  description = "ID of the RDS instance"
  # We use .identifier instead of the default .id, as CloudWatch 
  # requires a human-readable name for metrics, not an AWS ARN.
  value       = aws_db_instance.postgresql.identifier
}