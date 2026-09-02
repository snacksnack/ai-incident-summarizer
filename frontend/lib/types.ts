export interface SourceAlert {
  alert_id: string;
  source: string;
  alert_name: string;
  severity: string;
  status: string;
  received_at: string;
  monitor_id?: string;
}

export interface LlmSummary {
  summary: string;
  likely_cause: string;
  next_step: string;
}

export interface Service {
  affected_service: string;
  first_seen_at?: string;
  last_seen_at?: string;
}

export interface Incident {
  incident_id: string;
  affected_service: string;
  severity: string;
  status: string;
  created_at: string;
  source_alerts: SourceAlert[];
  llm_summary?: string;
  recovery_summary?: string;
  resolved_at?: string;
  slack_thread_id?: string;
  jira_ticket_id?: string;
}
