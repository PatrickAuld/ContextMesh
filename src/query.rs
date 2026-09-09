use crate::{
    api::App,
    auth::Identity,
    db,
    domain::{Record, ScopeFilter},
    error::{Error, Result},
    policy, records,
};
use axum::{extract::State, Extension, Json};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sqlx::Row;
use std::collections::HashSet;
use uuid::Uuid;

const MAX_SCAN: i64 = 4096;

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ContextRequest {
    pub task: String,
    #[serde(default)]
    pub scopes: Vec<ScopeFilter>,
    #[serde(default)]
    pub starting_records: Vec<Uuid>,
    #[serde(default = "default_max_tokens")]
    pub max_tokens: usize,
    #[serde(default)]
    pub purpose: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ContextResponse {
    pub context_text: String,
    pub records: Vec<Record>,
    pub guidance: Vec<String>,
    pub watermark: i64,
    pub truncated: bool,
}

fn default_max_tokens() -> usize {
    2048
}

pub async fn context(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Json(request): Json<ContextRequest>,
) -> Result<Json<ContextResponse>> {
    validate(&request)?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let state = sqlx::query("SELECT security_epoch, coalesce((SELECT max(sequence) FROM records WHERE tenant_id=$1),0) AS watermark FROM tenants WHERE id=$1")
        .bind(who.tenant).fetch_one(&mut *tx).await?;
    let epoch: i64 = state.get("security_epoch");
    let watermark: i64 = state.get("watermark");
    if epoch != who.security_epoch {
        return Err(Error::conflict("context_changed_retry"));
    }
    // This bounded helper authorizes complete transitive lineage and owns canonical
    // supersession/staleness filtering before records enter planning.
    let mut candidates = records::canonical_available(
        &mut tx,
        &who,
        &request.task,
        &request.scopes,
        &request.starting_records,
        MAX_SCAN,
    )
    .await?;
    tx.commit().await?;
    let scan_full = candidates.len() == MAX_SCAN as usize;
    rank(&mut candidates, &request.task, &request.starting_records);

    let (approved, _) = match &request.purpose {
        Some(purpose) => policy::evaluate(&app, &who, purpose, &request.task).await?,
        None => (Vec::new(), Vec::new()),
    };
    let mut context_text = String::new();
    let mut selected = Vec::new();
    let mut truncated = scan_full;
    // The public budget unit is a conservative UTF-8 byte cap for context_text.
    for record in candidates {
        let line = format!("[record {}]\n{}\n", record.id, record.content);
        if context_text.len() + line.len() > request.max_tokens {
            truncated = true;
            continue;
        }
        context_text.push_str(&line);
        selected.push(record);
    }
    let mut guidance = Vec::new();
    for output in approved {
        let line = format!("[approved guidance]\n{output}\n");
        if context_text.len() + line.len() > request.max_tokens {
            truncated = true;
            continue;
        }
        context_text.push_str(&line);
        guidance.push(output);
    }

    let mut tx = db::begin(&app.pool, who.tenant).await?;
    if db::lock_as(&mut tx, &who).await? != epoch {
        return Err(Error::conflict("context_changed_retry"));
    }
    db::audit(&mut tx, &who, "context.complete", None, json!({"record_count":selected.len(),"guidance_count":guidance.len(),"watermark":watermark})).await?;
    tx.commit().await?;
    Ok(Json(ContextResponse {
        context_text,
        records: selected,
        guidance,
        watermark,
        truncated,
    }))
}

fn validate(request: &ContextRequest) -> Result<()> {
    if request.task.trim().is_empty()
        || request.task.len() > 8000
        || request.scopes.len() > 32
        || request.starting_records.len() > 64
        || !(1..=32_768).contains(&request.max_tokens)
        || request
            .purpose
            .as_ref()
            .is_some_and(|p| p.is_empty() || p.len() > 128)
    {
        return Err(Error::bad("invalid_context_request"));
    }
    let unique: HashSet<_> = request.starting_records.iter().collect();
    if unique.len() != request.starting_records.len() {
        return Err(Error::bad("invalid_context_request"));
    }
    if request.scopes.iter().any(|scope| {
        scope
            .conversation
            .as_ref()
            .is_some_and(|v| v.is_empty() || v.len() > 512)
            || scope
                .project
                .as_ref()
                .is_some_and(|v| v.is_empty() || v.len() > 512)
            || scope.conversation.is_none() && scope.project.is_none()
    }) {
        return Err(Error::bad("invalid_context_request"));
    }
    Ok(())
}

fn rank(records: &mut [Record], task: &str, starting: &[Uuid]) {
    let terms: HashSet<String> = task
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| s.len() > 1)
        .map(str::to_lowercase)
        .collect();
    let starts: HashSet<Uuid> = starting.iter().copied().collect();
    records.sort_by_key(|record| {
        let content = record.content.to_lowercase();
        let matches = terms
            .iter()
            .filter(|term| content.contains(term.as_str()))
            .count();
        (
            !starts.contains(&record.id),
            std::cmp::Reverse(matches),
            std::cmp::Reverse(record.sequence),
        )
    });
}
