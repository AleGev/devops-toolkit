output "all_servers_connections" {
    description = "Command to connect to all servers"
    value       = { for name, server in module.application_servers : name => "ssh -i ~/.ssh/github_actions_key ec2-user@${server.private_ip}"
    } 
}