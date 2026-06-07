variable "db_identifier" {
  type        = string
  description = "Уникальное имя инстанса базы данных"
}

variable "environment" {
  type        = string
  description = "Имя среды для формирования пути в SSM Parameter Store"
}

variable "iam_role_id" {
  type        = string
  description = "ID роли IAM, которая будет использоваться для доступа к параметрам базы данных"

}