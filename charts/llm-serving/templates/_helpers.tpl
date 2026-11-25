{{- define "llm.labels" -}}
app.kubernetes.io/part-of: kube-llm-ops
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
