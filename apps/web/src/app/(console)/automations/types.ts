export type Versioned = {
  namespace: string;
  version: number;
  value: Record<string, unknown>;
  changed_by: string;
  reason: string;
  created_at: string | null;
  source: "platform_default" | "tenant";
  default: Record<string, unknown>;
};

export type PolicyView = {
  contact_window: [string, string];
  lending_window: [string, string];
  mfi_window: [string, string];
  max_contacts_7d: number;
  refund_approval_above_minor: number;
  representment_approval_above_minor: number;
  discount_approval_above_minor: number;
  voice_registered: boolean;
  lending_collections_enabled: boolean;
  allow_debit_shift: boolean;
  predebit_notice_hours: number;
  [k: string]: unknown;
};

export type SettingsResponse = {
  namespaces: Record<"channels" | "strategy" | "effects" | "experiments" | "policy", Versioned>;
  effective: {
    risk_threshold: number;
    capacity: Record<string, number>;
    effects: Record<string, Record<string, number>>;
    cost_minor: Record<string, number>;
    sources: Record<string, string>;
    policy: PolicyView;
  };
  policy_platform: PolicyView;
  can_edit: boolean;
};

export type CategoryEvidence = {
  source: "learned" | "prior";
  why?: string;
  treated: number;
  holdout: number;
  contacted: Record<string, number>;
  recovery_treated?: number;
  recovery_holdout?: number;
  itt?: number;
  cace?: number;
  cace_ci95?: [number, number];
  effective_n?: number;
};

export type LearnedResponse = {
  effects: null | {
    version: number;
    created_at: string;
    value: { effects: Record<string, Record<string, number>> };
    evidence: { outcomes_used: number; method: string; categories: Record<string, CategoryEvidence> };
  };
  risk_threshold: null | {
    version: number;
    created_at: string;
    value: { threshold: number | null };
    evidence: {
      labels: number; positives: number; why?: string; threshold?: number; precision?: number | null;
      recall?: number; value_minor_at_threshold?: number; value_minor_at_fallback?: number; fallback: number;
    };
  };
};

export type RiskThresholdSettings = {
  mode: "learned" | "fixed"; fixed: number; min_labels: number; min_positives: number;
  flag_cost_minor: number; prevention_share: number;
};
