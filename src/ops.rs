use crate::{
    api::App,
    auth::Identity,
    db,
    error::{Error, Result},
};
use axum::{
    extract::{Path, Query, State},
    Extension, Json,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::Row;
use uuid::Uuid;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Delegate {
    pub name: String,
    #[serde(default = "ttl")]
    pub ttl_seconds: i64,
}
fn ttl() -> i64 {
    3600
}
pub async fn delegate(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Json(input): Json<Delegate>,
) -> Result<Json<Value>> {
    if who.agent.is_some() {
        return Err(Error::forbidden());
    }
    if input.name.is_empty() || input.name.len() > 128 || !(60..=86400).contains(&input.ttl_seconds)
    {
        return Err(Error::bad("invalid_delegation"));
    }
    let id = Uuid::new_v4();
    let token = format!(
        "cm_{}_{}{}",
        who.tenant,
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple()
    );
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    sqlx::query("INSERT INTO agent_tokens(tenant_id,id,token_hash,owner,name,expires_at) VALUES($1,$2,$3,$4,$5,now()+make_interval(secs=>$6::int))")
        .bind(who.tenant).bind(id).bind(db::hash(&token)).bind(&who.subject).bind(input.name).bind(input.ttl_seconds as i32).execute(&mut *tx).await?;
    db::audit(
        &mut tx,
        &who,
        "agent.delegate",
        Some(id),
        json!({"ttl_seconds":input.ttl_seconds}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(
        json!({"agent_id":id,"token":token,"ttl_seconds":input.ttl_seconds}),
    ))
}
pub async fn revoke_agent(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    if who.agent.is_some() {
        return Err(Error::forbidden());
    }
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let r = sqlx::query(
        "UPDATE agent_tokens SET revoked=true WHERE tenant_id=$1 AND id=$2 AND (owner=$3 OR $4)",
    )
    .bind(who.tenant)
    .bind(id)
    .bind(&who.subject)
    .bind(who.admin)
    .execute(&mut *tx)
    .await?;
    if r.rows_affected() == 0 {
        return Err(Error::missing());
    }
    sqlx::query("UPDATE tenants SET security_epoch=security_epoch+1 WHERE id=$1")
        .bind(who.tenant)
        .execute(&mut *tx)
        .await?;
    db::audit(&mut tx, &who, "agent.revoke", Some(id), json!({})).await?;
    tx.commit().await?;
    Ok(Json(json!({"revoked":id})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrincipalChange {
    pub subject: String,
    pub disabled: bool,
}
pub async fn principal(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Json(input): Json<PrincipalChange>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    if input.subject == who.subject {
        return Err(Error::bad("cannot_disable_self"));
    }
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let n = sqlx::query("UPDATE principals SET disabled=$3 WHERE tenant_id=$1 AND subject=$2")
        .bind(who.tenant)
        .bind(&input.subject)
        .bind(input.disabled)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    if n == 0 {
        return Err(Error::missing());
    }
    sqlx::query("UPDATE tenants SET security_epoch=security_epoch+1 WHERE id=$1")
        .bind(who.tenant)
        .execute(&mut *tx)
        .await?;
    db::audit(
        &mut tx,
        &who,
        "principal.status",
        None,
        json!({"subject":input.subject,"disabled":input.disabled}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"updated":true})))
}
#[derive(Deserialize)]
pub struct Page {
    #[serde(default)]
    pub after: i64,
    #[serde(default)]
    pub target: Option<Uuid>,
}
pub async fn audit(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Query(page): Query<Page>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let rows=sqlx::query("SELECT * FROM audit WHERE tenant_id=$1 AND sequence>$2 AND ($3::uuid IS NULL OR target=$3) ORDER BY sequence LIMIT 200")
        .bind(who.tenant).bind(page.after).bind(page.target).fetch_all(&mut *tx).await?;
    Ok(Json(
        json!({"entries":rows.iter().map(|r|json!({"sequence":r.get::<i64,_>("sequence"),"actor":r.get::<String,_>("actor"),"agent_id":r.get::<Option<Uuid>,_>("agent_id"),"action":r.get::<String,_>("action"),"target":r.get::<Option<Uuid>,_>("target"),"metadata":r.get::<Value,_>("metadata"),"created_at":r.get::<chrono::DateTime<chrono::Utc>,_>("created_at")})).collect::<Vec<_>>(),"next_after":rows.last().map(|r|r.get::<i64,_>("sequence"))}),
    ))
}
pub async fn status(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let rows =
        sqlx::query("SELECT state,count(*) AS count FROM jobs WHERE tenant_id=$1 GROUP BY state")
            .bind(who.tenant)
            .fetch_all(&mut *tx)
            .await?;
    let mut counts = serde_json::Map::new();
    for r in rows {
        counts.insert(r.get("state"), json!(r.get::<i64, _>("count")));
    }
    Ok(Json(
        json!({"tenant_id":who.tenant,"jobs":counts,"gateway_configured":app.gateway.configured()}),
    ))
}
pub async fn jobs(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let rows=sqlx::query("SELECT id,record_id,state,attempts,lease_until,error_code FROM jobs WHERE tenant_id=$1 AND state IN ('failed','running','pending') ORDER BY available_at LIMIT 200").bind(who.tenant).fetch_all(&mut *tx).await?;
    Ok(Json(
        json!({"jobs":rows.iter().map(|r|json!({"id":r.get::<Uuid,_>("id"),"record_id":r.get::<Uuid,_>("record_id"),"state":r.get::<String,_>("state"),"attempts":r.get::<i32,_>("attempts"),"lease_until":r.get::<Option<chrono::DateTime<chrono::Utc>>,_>("lease_until"),"error_code":r.get::<Option<String>,_>("error_code")})).collect::<Vec<_>>()}),
    ))
}

pub async fn metrics(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
) -> Result<([(axum::http::HeaderName, &'static str); 1], String)> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let rows =
        sqlx::query("SELECT state,count(*) AS count FROM jobs WHERE tenant_id=$1 GROUP BY state")
            .bind(who.tenant)
            .fetch_all(&mut *tx)
            .await?;
    let mut output=String::from("# HELP contextmesh_jobs Durable jobs by state for the authenticated tenant.\n# TYPE contextmesh_jobs gauge\n");
    for row in rows {
        output.push_str(&format!(
            "contextmesh_jobs{{state=\"{}\"}} {}\n",
            row.get::<String, _>("state"),
            row.get::<i64, _>("count")
        ));
    }
    let age:f64=sqlx::query_scalar("SELECT coalesce(extract(epoch FROM now()-min(available_at))::float8,0) FROM jobs WHERE tenant_id=$1 AND state='pending'").bind(who.tenant).fetch_one(&mut *tx).await?;
    output.push_str(&format!("# HELP contextmesh_queue_age_seconds Oldest available pending job age.\n# TYPE contextmesh_queue_age_seconds gauge\ncontextmesh_queue_age_seconds {}\n",age.max(0.0)));
    Ok((
        [(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4",
        )],
        output,
    ))
}

pub async fn redactions(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Query(page): Query<Page>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock_as(&mut tx, &who).await?;
    let rows=sqlx::query("SELECT id,sequence FROM records WHERE tenant_id=$1 AND redacted AND sequence>$2 ORDER BY sequence LIMIT 200").bind(who.tenant).bind(page.after).fetch_all(&mut *tx).await?;
    Ok(Json(
        json!({"tenant_id":who.tenant,"records":rows.iter().map(|r|json!({"record_id":r.get::<Uuid,_>("id"),"sequence":r.get::<i64,_>("sequence")})).collect::<Vec<_>>(),"next_after":rows.last().map(|r|r.get::<i64,_>("sequence"))}),
    ))
}
