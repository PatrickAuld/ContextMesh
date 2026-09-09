use crate::{
    api::App,
    auth::Identity,
    db,
    domain::{Derivation, NewRecord, Scope, Support},
    error::Result,
    inference::{CuratedRecord, CURATOR_INSTRUCTIONS},
};
use serde_json::{json, Map, Value};
use sqlx::Row;
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

const MAX_ATTEMPTS: i32 = 5;
const MAX_CONVERSATION: i64 = 128;
const MAX_NOTES: i64 = 8;
const MAX_MANIFEST: usize = 256;
const MAX_REQUEST_BYTES: usize = 1_000_000;
const EXTRACTOR_VERSION: &str = "records-v1";

struct Lease {
    job: Uuid,
    lease: Uuid,
    source: Uuid,
    epoch: i64,
}

struct Input {
    author: String,
    groups: Vec<String>,
    scope: Scope,
    manifest: Vec<Uuid>,
    contents: HashMap<Uuid, String>,
    request: Value,
    coverage: Value,
}

pub async fn run(app: App, mut stop: tokio::sync::watch::Receiver<bool>) {
    let mut turn = 0usize;
    loop {
        if *stop.borrow() {
            break;
        }
        let mut worked = false;
        for offset in 0..app.config.tenants.len() {
            let tenant = app.config.tenants[(turn + offset) % app.config.tenants.len()].id;
            match process(&app, tenant).await {
                Ok(true) => {
                    worked = true;
                    break;
                }
                Ok(false) => {}
                Err(_) => tracing::error!(%tenant, "curator database operation failed"),
            }
        }
        if !app.config.tenants.is_empty() {
            turn = (turn + 1) % app.config.tenants.len();
        }
        if !worked {
            tokio::select! {
                _ = tokio::time::sleep(std::time::Duration::from_millis(250)) => {},
                _ = stop.changed() => {}
            }
        }
    }
}

async fn process(app: &App, tenant: Uuid) -> Result<bool> {
    let Some(lease) = claim(app, tenant).await? else {
        return Ok(false);
    };
    if !app.gateway.configured() {
        finish_without_inference(app, tenant, &lease, "gateway_unconfigured").await?;
        return Ok(true);
    }
    let input = match assemble(app, tenant, &lease).await? {
        Some(input) => input,
        None => {
            cancel(app, tenant, &lease, "source_unavailable").await?;
            return Ok(true);
        }
    };
    let result = app.gateway.curate(input.request.clone()).await;
    complete(app, tenant, lease, input, result).await?;
    Ok(true)
}

async fn claim(app: &App, tenant: Uuid) -> Result<Option<Lease>> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    sqlx::query("UPDATE jobs SET state='failed',error_code='lease_exhausted',lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND state='running' AND lease_until<now() AND attempts>=$2")
        .bind(tenant).bind(MAX_ATTEMPTS).execute(&mut *tx).await?;
    let lease = Uuid::new_v4();
    let row = sqlx::query("UPDATE jobs SET state='running',attempts=attempts+1,lease_id=$2,lease_until=now()+interval '60 seconds',error_code=NULL WHERE tenant_id=$1 AND id=(SELECT id FROM jobs WHERE tenant_id=$1 AND attempts<$3 AND ((state='pending' AND available_at<=now()) OR (state='running' AND lease_until<now())) ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,record_id")
        .bind(tenant).bind(lease).bind(MAX_ATTEMPTS).fetch_optional(&mut *tx).await?;
    let epoch: i64 = sqlx::query_scalar("SELECT security_epoch FROM tenants WHERE id=$1")
        .bind(tenant)
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(row.map(|row| Lease {
        job: row.get("id"),
        source: row.get("record_id"),
        lease,
        epoch,
    }))
}

async fn assemble(app: &App, tenant: Uuid, lease: &Lease) -> Result<Option<Input>> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    let source = sqlx::query("SELECT author,scope,sequence FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted AND derivation IS NULL")
        .bind(tenant).bind(lease.source).fetch_optional(&mut *tx).await?;
    let Some(source) = source else {
        tx.commit().await?;
        return Ok(None);
    };
    let author: String = source.get("author");
    let principal =
        sqlx::query("SELECT groups,disabled FROM principals WHERE tenant_id=$1 AND subject=$2")
            .bind(tenant)
            .bind(&author)
            .fetch_optional(&mut *tx)
            .await?;
    let Some(principal) = principal else {
        tx.commit().await?;
        return Ok(None);
    };
    if principal.get::<bool, _>("disabled") {
        tx.commit().await?;
        return Ok(None);
    }
    let actor_groups: Vec<String> = principal.get("groups");
    let scope_value: Value = source.get("scope");
    let scope: Scope = serde_json::from_value(scope_value.clone())
        .map_err(|_| crate::error::Error::bad("invalid_scope"))?;
    let validator = Identity {
        tenant,
        subject: author.clone(),
        groups: actor_groups,
        admin: false,
        agent: None,
        security_epoch: lease.epoch,
    };
    let sequence: i64 = source.get("sequence");
    let total: i64 = sqlx::query_scalar("SELECT count(*) FROM records WHERE tenant_id=$1 AND derivation IS NULL AND NOT redacted AND sequence<=$2 AND (($3::text IS NOT NULL AND scope->>'conversation'=$3) OR id=$4)")
        .bind(tenant).bind(sequence).bind(scope.conversation.as_deref()).bind(lease.source).fetch_one(&mut *tx).await?;
    let mut conversation = sqlx::query("SELECT id,sequence,content,metadata,recorded_at,inputs,supports,scope FROM records WHERE tenant_id=$1 AND derivation IS NULL AND NOT redacted AND sequence<=$2 AND (($3::text IS NOT NULL AND scope->>'conversation'=$3) OR id=$4) ORDER BY sequence DESC LIMIT $5")
        .bind(tenant).bind(sequence).bind(scope.conversation.as_deref()).bind(lease.source).bind(MAX_CONVERSATION).fetch_all(&mut *tx).await?;
    conversation.reverse();
    let mut current_conversation = Vec::new();
    for row in conversation {
        let id: Uuid = row.get("id");
        if crate::records::record_is_current_available(&mut tx, &validator, id).await? {
            current_conversation.push(row);
        } else if id == lease.source {
            tx.commit().await?;
            return Ok(None);
        }
    }
    let conversation = current_conversation;
    let note_candidates = sqlx::query("SELECT r.id,r.sequence,r.content,r.metadata,r.recorded_at,r.inputs,r.supports,r.scope FROM records r WHERE r.tenant_id=$1 AND r.derivation IS NOT NULL AND NOT r.redacted AND r.sequence<$2 AND $3::text IS NOT NULL AND r.scope->>'project'=$3 ORDER BY r.sequence DESC LIMIT $4")
        .bind(tenant).bind(sequence).bind(scope.project.as_deref()).bind(MAX_NOTES).fetch_all(&mut *tx).await?;
    let mut notes = Vec::new();
    for row in note_candidates {
        if crate::records::record_is_current_available(&mut tx, &validator, row.get("id")).await? {
            notes.push(row);
        }
    }
    notes.reverse();
    tx.commit().await?;
    let mut manifest = Vec::new();
    let mut seen = HashSet::new();
    let mut contents = HashMap::new();
    for row in conversation.iter().chain(notes.iter()) {
        let id: Uuid = row.get("id");
        if seen.insert(id) {
            manifest.push(id);
            contents.insert(id, row.get("content"));
        }
    }
    if !seen.contains(&lease.source) || manifest.len() > MAX_MANIFEST {
        return Ok(None);
    }
    let record_json = |row: &sqlx::postgres::PgRow| {
        json!({
            "id":row.get::<Uuid,_>("id"), "sequence":row.get::<i64,_>("sequence"),
            "content":row.get::<String,_>("content"), "metadata":row.get::<Value,_>("metadata"),
            "recorded_at":row.get::<chrono::DateTime<chrono::Utc>,_>("recorded_at"),
            "inputs":row.get::<Vec<Uuid>,_>("inputs"), "supports":row.get::<Value,_>("supports")
        })
    };
    let first_sequence = conversation.first().map(|r| r.get::<i64, _>("sequence"));
    let coverage = json!({
        "kind":"bounded_segment", "through_sequence":sequence, "first_sequence":first_sequence,
        "conversation_record_count":conversation.len(), "conversation_total_through_source":total,
        "truncated":total > conversation.len() as i64, "complete":total <= conversation.len() as i64
    });
    let request = json!({
        "conversation_records": conversation.iter().map(record_json).collect::<Vec<_>>(),
        "relevant_notes": notes.iter().map(record_json).collect::<Vec<_>>(),
        "supporting_records": Vec::<Value>::new(),
        "input_manifest":manifest, "coverage":coverage, "instructions":CURATOR_INSTRUCTIONS
    });
    if serde_json::to_vec(&request).map_or(true, |bytes| bytes.len() > MAX_REQUEST_BYTES) {
        return Ok(None);
    }
    Ok(Some(Input {
        author,
        groups: validator.groups,
        scope,
        manifest,
        contents,
        request,
        coverage,
    }))
}

async fn complete(
    app: &App,
    tenant: Uuid,
    lease: Lease,
    input: Input,
    result: anyhow::Result<Vec<CuratedRecord>>,
) -> Result<()> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    let current_epoch = db::lock(&mut tx, tenant).await?;
    let valid_job: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM jobs WHERE tenant_id=$1 AND id=$2 AND record_id=$3 AND state='running' AND lease_id=$4 AND lease_until>now())")
        .bind(tenant).bind(lease.job).bind(lease.source).bind(lease.lease).fetch_one(&mut *tx).await?;
    let valid_inputs: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM records WHERE tenant_id=$1 AND id=ANY($2) AND NOT redacted",
    )
    .bind(tenant)
    .bind(&input.manifest)
    .fetch_one(&mut *tx)
    .await?;
    let validator = Identity {
        tenant,
        subject: input.author.clone(),
        groups: input.groups.clone(),
        admin: false,
        agent: None,
        security_epoch: current_epoch,
    };
    let mut lineage_valid = true;
    for id in &input.manifest {
        if !crate::records::record_is_current_available(&mut tx, &validator, *id).await? {
            lineage_valid = false;
            break;
        }
    }
    if !valid_job
        || current_epoch != lease.epoch
        || valid_inputs as usize != input.manifest.len()
        || !lineage_valid
    {
        tx.rollback().await?;
        return Ok(());
    }
    match result.and_then(|records| validate_outputs(records, &input)) {
        Ok(records) => {
            let who = validator;
            let derivation = Derivation {
                model: app.gateway.model().unwrap_or_default(),
                extractor_version: EXTRACTOR_VERSION.into(),
                instructions: CURATOR_INSTRUCTIONS.into(),
            };
            let mut derived = Vec::new();
            for output in records {
                let mut metadata = match output.metadata {
                    Value::Object(v) => v,
                    _ => Map::new(),
                };
                metadata.insert(
                    "curation".into(),
                    json!({"source_record_id":lease.source,"coverage":input.coverage}),
                );
                derived.push(NewRecord {
                    id: Uuid::new_v4(),
                    content: output.content,
                    scope: input.scope.clone(),
                    inputs: input.manifest.clone(),
                    supports: output
                        .supports
                        .into_iter()
                        .map(|s| Support {
                            record_id: s.record_id,
                            quote: s.quote,
                        })
                        .collect(),
                    supersedes: vec![],
                    metadata: Value::Object(metadata),
                });
            }
            if !derived.is_empty() {
                crate::records::append_in_transaction(
                    &mut tx,
                    &who,
                    derived,
                    Some(derivation),
                    false,
                )
                .await?;
            }
            sqlx::query("UPDATE jobs SET state='done',error_code=NULL,lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3")
                .bind(tenant).bind(lease.job).bind(lease.lease).execute(&mut *tx).await?;
            db::audit(
                &mut tx,
                &who,
                "record.curate",
                Some(lease.source),
                json!({"job_id":lease.job}),
            )
            .await?;
        }
        Err(_) => retry(&mut tx, tenant, &lease).await?,
    }
    tx.commit().await?;
    Ok(())
}

fn validate_outputs(
    records: Vec<CuratedRecord>,
    input: &Input,
) -> anyhow::Result<Vec<CuratedRecord>> {
    for record in &records {
        anyhow::ensure!(!record.supports.is_empty(), "missing_supports");
        for support in &record.supports {
            let source = input
                .contents
                .get(&support.record_id)
                .ok_or_else(|| anyhow::anyhow!("support_outside_manifest"))?;
            anyhow::ensure!(
                !support.quote.is_empty() && source.contains(&support.quote),
                "invalid_support"
            );
        }
    }
    Ok(records)
}

async fn retry(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    tenant: Uuid,
    lease: &Lease,
) -> Result<()> {
    sqlx::query("UPDATE jobs SET state=CASE WHEN attempts>=$4 THEN 'failed' ELSE 'pending' END,error_code='curation_failed',available_at=now()+make_interval(secs=>least(60,power(2,attempts)::int)),lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3")
        .bind(tenant).bind(lease.job).bind(lease.lease).bind(MAX_ATTEMPTS).execute(&mut **tx).await?;
    Ok(())
}

async fn cancel(app: &App, tenant: Uuid, lease: &Lease, code: &str) -> Result<()> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    sqlx::query("UPDATE jobs SET state='cancelled',error_code=$4,lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3")
        .bind(tenant).bind(lease.job).bind(lease.lease).bind(code).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(())
}

async fn finish_without_inference(
    app: &App,
    tenant: Uuid,
    lease: &Lease,
    code: &str,
) -> Result<()> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    let epoch = db::lock(&mut tx, tenant).await?;
    let valid: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM jobs j JOIN records r ON r.tenant_id=j.tenant_id AND r.id=j.record_id WHERE j.tenant_id=$1 AND j.id=$2 AND j.lease_id=$3 AND j.state='running' AND j.lease_until>now() AND NOT r.redacted AND r.derivation IS NULL)")
        .bind(tenant).bind(lease.job).bind(lease.lease).fetch_one(&mut *tx).await?;
    if valid && epoch == lease.epoch {
        sqlx::query("UPDATE jobs SET state='done',error_code=$4,lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3")
            .bind(tenant).bind(lease.job).bind(lease.lease).bind(code).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(())
}
