output "redis_cluster_id" {
  description = "ID of the Redis cluster"
  value       = aws_elasticache_cluster.redis_node.id
}

output "redis_security_group_id" {
  description = "ID of the Redis security group"
  value       = aws_security_group.redis.id
}


# 5. Вывод адреса подключения для приложения
output "redis_endpoint" {
  description = "Connection endpoint for Redis"
  value       = aws_elasticache_cluster.redis_node.cache_nodes[0].address
}