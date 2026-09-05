use crate::{
    api::App,
    auth::Identity,
    db,
    error::{Error, Result},
};
use axum::{
    extract::{Path, State},
    Extension, Json,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::Row;
use uuid::Uuid;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Insert {
    pub source: String,
    pub external_id: String,
    pub revision: i64,
    pub text: String,
    #[serde(default = "object")]
    pub context: Value,
    #[serde(default = "internal")]
    pub classification: String,
    #[serde(default)]
    pub read_groups: Vec<String>,
}
pub fn object() -> Value {
    json!({})
}
fn internal() -> String {
    "internal".into()
}
pub async fn insert(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Json(input): Json<Insert>,
) -> Result<Json<Value>> {
    if input.source.is_empty()
        || input.source.len() > 128
        || input.external_id.is_empty()
        || input.external_id.len() > 512
        || input.revision < 1
        || input.text.is_empty()
        || input.text.len() > 65536
        || !input.context.is_object()
        || input.context.to_string().len() > 16000
        || !["internal", "restricted"].contains(&input.classification.as_str())
        || input.read_groups.len() > 64
    {
        return Err(Error::bad("invalid_event"));
    }
    let hash = db::hash(&serde_json::to_string(&input).unwrap());
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let existing=sqlx::query("SELECT id,input_hash,actor,redacted FROM events WHERE tenant_id=$1 AND source=$2 AND external_id=$3 AND revision=$4")
        .bind(who.tenant).bind(&input.source).bind(&input.external_id).bind(input.revision).fetch_optional(&mut *tx).await?;
    if let Some(row) = existing {
        if row.get::<String, _>("actor") != who.subject && !who.admin {
            return Err(Error::forbidden());
        }
        if row.get::<bool, _>("redacted") {
            return Err(Error::conflict("source_redacted"));
        }
        if row.get::<String, _>("input_hash") != hash {
            return Err(Error::conflict("revision_content_mismatch"));
        }
        return Ok(Json(
            json!({"event_id":row.get::<Uuid,_>("id"),"duplicate":true}),
        ));
    }
    let head=sqlx::query("SELECT id,revision,actor,redacted,classification,read_groups FROM events WHERE tenant_id=$1 AND source=$2 AND external_id=$3 AND current")
        .bind(who.tenant).bind(&input.source).bind(&input.external_id).fetch_optional(&mut *tx).await?;
    let mut classification = input.classification.clone();
    let mut groups = input.read_groups.clone();
    if let Some(row) = head {
        if row.get::<String, _>("actor") != who.subject && !who.admin {
            return Err(Error::forbidden());
        }
        if row.get::<bool, _>("redacted") {
            return Err(Error::conflict("source_redacted"));
        }
        if row.get::<i64, _>("revision") >= input.revision {
            return Err(Error::conflict("stale_revision"));
        }
        if !who.admin && row.get::<String, _>("classification") == "restricted" {
            classification = "restricted".into();
            groups = row.get("read_groups");
        }
        let old: Uuid = row.get("id");
        sqlx::query("UPDATE events SET current=false WHERE tenant_id=$1 AND id=$2")
            .bind(who.tenant)
            .bind(old)
            .execute(&mut *tx)
            .await?;
        invalidate(&mut tx, who.tenant, &[old]).await?;
        sqlx::query("UPDATE jobs SET state='cancelled',lease_id=NULL WHERE tenant_id=$1 AND event_id=$2 AND state IN ('pending','running','failed')").bind(who.tenant).bind(old).execute(&mut *tx).await?;
    }
    let backlog: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM jobs WHERE tenant_id=$1 AND state IN ('pending','running')",
    )
    .bind(who.tenant)
    .fetch_one(&mut *tx)
    .await?;
    if backlog >= 100000 {
        return Err(Error(
            axum::http::StatusCode::TOO_MANY_REQUESTS,
            "ingestion_backlog_limit",
        ));
    }
    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO events(tenant_id,id,source,external_id,revision,actor,agent_id,body,context,input_hash,classification,read_groups) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)")
        .bind(who.tenant).bind(id).bind(&input.source).bind(&input.external_id).bind(input.revision).bind(&who.subject).bind(who.agent).bind(&input.text).bind(&input.context).bind(hash).bind(classification).bind(groups).execute(&mut *tx).await?;
    let graphs: Vec<Uuid> =
        sqlx::query_scalar("SELECT id FROM graphs WHERE tenant_id=$1 AND state<>'archived'")
            .bind(who.tenant)
            .fetch_all(&mut *tx)
            .await?;
    for graph in graphs {
        sqlx::query("INSERT INTO jobs(tenant_id,id,graph_id,event_id) VALUES($1,$2,$3,$4)")
            .bind(who.tenant)
            .bind(Uuid::new_v4())
            .bind(graph)
            .bind(id)
            .execute(&mut *tx)
            .await?;
    }
    db::audit(
        &mut tx,
        &who,
        "event.insert",
        Some(id),
        json!({"revision":input.revision}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"event_id":id,"duplicate":false})))
}
pub async fn get(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    let r = sqlx::query("SELECT * FROM events WHERE tenant_id=$1 AND id=$2 AND NOT redacted")
        .bind(who.tenant)
        .bind(id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(Error::missing)?;
    if !who.can_read(
        r.get("classification"),
        &r.get::<Vec<String>, _>("read_groups"),
    ) || (!r.get::<bool, _>("current") && !who.admin)
    {
        return Err(Error::missing());
    }
    db::audit(&mut tx, &who, "event.read", Some(id), json!({})).await?;
    let output = json!({"id":id,"text":r.get::<Option<String>,_>("body"),"context":r.get::<Value,_>("context"),"source":r.get::<String,_>("source"),"external_id":r.get::<String,_>("external_id"),"revision":r.get::<i64,_>("revision"),"actor":r.get::<String,_>("actor"),"agent_id":r.get::<Option<Uuid>,_>("agent_id"),"classification":r.get::<String,_>("classification"),"read_groups":r.get::<Vec<String>,_>("read_groups"),"current":r.get::<bool,_>("current")});
    tx.commit().await?;
    Ok(Json(output))
}
pub async fn invalidate(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    tenant: Uuid,
    ids: &[Uuid],
) -> Result<()> {
    sqlx::query("UPDATE policies SET enabled=false WHERE tenant_id=$1 AND evidence && $2")
        .bind(tenant)
        .bind(ids)
        .execute(&mut **tx)
        .await?;
    sqlx::query("UPDATE tenants SET security_epoch=security_epoch+1 WHERE id=$1")
        .bind(tenant)
        .execute(&mut **tx)
        .await?;
    Ok(())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Classify {
    pub classification: String,
    #[serde(default)]
    pub read_groups: Vec<String>,
}
pub async fn classify(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
    Json(input): Json<Classify>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    if !["internal", "restricted"].contains(&input.classification.as_str())
        || input.read_groups.len() > 64
    {
        return Err(Error::bad("invalid_classification"));
    }
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let ids:Vec<Uuid>=sqlx::query_scalar("UPDATE events SET classification=$3,read_groups=$4 WHERE tenant_id=$1 AND (source,external_id)=(SELECT source,external_id FROM events WHERE tenant_id=$1 AND id=$2 AND NOT redacted) AND NOT redacted RETURNING id")
        .bind(who.tenant).bind(id).bind(&input.classification).bind(&input.read_groups).fetch_all(&mut *tx).await?;
    if ids.is_empty() {
        return Err(Error::missing());
    }
    invalidate(&mut tx, who.tenant, &ids).await?;
    db::audit(
        &mut tx,
        &who,
        "event.classify",
        Some(id),
        json!({"classification":input.classification,"revisions":ids.len()}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"updated_revisions":ids.len(),"dependent_policies_disabled":true}),
    ))
}
pub async fn redact(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let ids:Vec<Uuid>=sqlx::query_scalar("UPDATE events SET body=NULL,context='{}',input_hash='',redacted=true WHERE tenant_id=$1 AND (source,external_id)=(SELECT source,external_id FROM events WHERE tenant_id=$1 AND id=$2) RETURNING id")
        .bind(who.tenant).bind(id).fetch_all(&mut *tx).await?;
    if ids.is_empty() {
        return Err(Error::missing());
    }
    invalidate(&mut tx, who.tenant, &ids).await?;
    sqlx::query("DELETE FROM claims WHERE tenant_id=$1 AND event_id=ANY($2)")
        .bind(who.tenant)
        .bind(&ids)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE jobs SET state='cancelled',lease_id=NULL,lease_until=NULL,error_code=NULL WHERE tenant_id=$1 AND event_id=ANY($2)").bind(who.tenant).bind(&ids).execute(&mut *tx).await?;
    sqlx::query("UPDATE policies SET outputs='{}',instruction='',enabled=false WHERE tenant_id=$1 AND evidence && $2").bind(who.tenant).bind(&ids).execute(&mut *tx).await?;
    db::audit(
        &mut tx,
        &who,
        "event.redact",
        Some(id),
        json!({"revisions":ids.len()}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"redacted_revisions":ids.len()})))
}
