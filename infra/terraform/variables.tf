# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
variable "project_id" {
  description = "The project id"
}

variable "secondary_project_id" {
  description = "Alternative project id, to accomodate the old chrome deployment"
}

variable "region" {
  description = "The region"
}

variable "subnet_name" {
  description = "The compute subnetwork name"
}

variable "network_name" {
  description = "The network name"
}

variable "ip_cidr_range" {
  description = "The IP CIDR range"
}

variable "gke_num_nodes" {
  default     = 5
  description = "The number of gke nodes"
}

variable "machine_type" {
  default     = "e2-standard-4"
  description = "The machine type"
}

variable "network_auto_mode" {
  default = "false"
  description = "The subnet creation mode"
}

variable "network_description" {
  default = ""
  description = "The network description"
}

variable "appengine_service_account" {
  default = ""
  description = "AppEngine service account for the clusterfuzz deployment"
}

variable "testcase_replication_push_endpoint" {
  default = ""
  description = "Endpoint to push testcase replication messages to"
}
