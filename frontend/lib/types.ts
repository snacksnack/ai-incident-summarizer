export interface SourceAlert {
  alert_id: string;
  source: string;
  alert_name: string;
  severity: string;
  status: string;
  received_at: string;
}

export interface LlmSummary {
  summary: string;
  likely_cause: string;
  next_step: string;
}

export interface Incident {
  incident_id: string;
  affected_service: string;
  severity: string;
  status: string;
  created_at: string;
  source_alerts: SourceAlert[];
  llm_summary?: string;
  slack_thread_id?: string;
  jira_ticket_id?: string;
}
