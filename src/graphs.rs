use crate::{
    api::App,
    auth::Identity,
    db,
    domain::GraphConfig,
    error::{Error, Result},
};
use axum::{
    extract::{Path, State},
    Extension, Json,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::Row;
use uuid::Uuid;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Create {
    pub name: String,
    #[serde(default)]
    pub config: GraphConfig,
}
pub async fn create(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Json(mut input): Json<Create>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    input.config.validate()?;
    if input.name.is_empty() || input.name.len() > 128 {
        return Err(Error::bad("invalid_graph_name"));
    }
    if input.config.mode == "llm" && !app.gateway.configured() {
        return Err(Error::bad("gateway_unconfigured"));
    }
    if input.config.mode == "llm" && input.config.model.is_none() {
        input.config.model = app.gateway.model();
    }
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM graphs WHERE tenant_id=$1 AND state<>'archived'")
            .bind(who.tenant)
            .fetch_one(&mut *tx)
            .await?;
    if count >= 8 {
        return Err(Error::conflict("maintained_graph_limit"));
    }
    let id = Uuid::new_v4();
    sqlx::query("INSERT INTO graphs(tenant_id,id,name,config,backfill_target) VALUES($1,$2,$3,$4,(SELECT coalesce(max(sequence),0) FROM events WHERE tenant_id=$1))").bind(who.tenant).bind(id).bind(&input.name).bind(serde_json::to_value(input.config).unwrap()).execute(&mut *tx).await?;
    let queued: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events WHERE tenant_id=$1 AND current AND NOT redacted",
    )
    .bind(who.tenant)
    .fetch_one(&mut *tx)
    .await?;
    if queued == 0 {
        sqlx::query("UPDATE graphs SET state='ready' WHERE tenant_id=$1 AND id=$2")
            .bind(who.tenant)
            .bind(id)
            .execute(&mut *tx)
            .await?;
    }
    db::audit(
        &mut tx,
        &who,
        "graph.create",
        Some(id),
        json!({"events":queued}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"graph_id":id,"queued":queued})))
}
pub async fn list(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
) -> Result<Json<Value>> {
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    let rows=sqlx::query("SELECT g.*,g.id=t.active_graph AS active,(SELECT count(*) FROM jobs j WHERE j.tenant_id=g.tenant_id AND j.graph_id=g.id AND j.state IN ('pending','running')) AS pending,(SELECT count(*) FROM jobs j WHERE j.tenant_id=g.tenant_id AND j.graph_id=g.id AND j.state='failed') AS failed FROM graphs g JOIN tenants t ON t.id=g.tenant_id WHERE g.tenant_id=$1 ORDER BY g.created_at DESC LIMIT 100").bind(who.tenant).fetch_all(&mut *tx).await?;
    Ok(Json(
        json!({"graphs":rows.iter().map(|r|json!({"id":r.get::<Uuid,_>("id"),"name":r.get::<String,_>("name"),"state":r.get::<String,_>("state"),"active":r.get::<Option<bool>,_>("active"),"pending":r.get::<i64,_>("pending"),"failed":r.get::<i64,_>("failed"),"config":if who.admin{r.get::<Value,_>("config")}else{Value::Null}})).collect::<Vec<_>>()}),
    ))
}
pub async fn promote(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let state: Option<String> =
        sqlx::query_scalar("SELECT state FROM graphs WHERE tenant_id=$1 AND id=$2")
            .bind(who.tenant)
            .bind(id)
            .fetch_optional(&mut *tx)
            .await?;
    if state.is_none() {
        return Err(Error::missing());
    }
    if state.as_deref() != Some("ready") {
        return Err(Error::conflict("graph_not_ready"));
    }
    let remaining:i64=sqlx::query_scalar("SELECT count(*) FROM jobs WHERE tenant_id=$1 AND graph_id=$2 AND state IN ('pending','running','failed')").bind(who.tenant).bind(id).fetch_one(&mut *tx).await?;
    if remaining > 0 {
        return Err(Error::conflict("graph_not_caught_up"));
    }
    sqlx::query("UPDATE tenants SET active_graph=$2,security_epoch=security_epoch+1 WHERE id=$1")
        .bind(who.tenant)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    db::audit(&mut tx, &who, "graph.promote", Some(id), json!({})).await?;
    tx.commit().await?;
    Ok(Json(json!({"active_graph":id})))
}
pub async fn archive(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let result=sqlx::query("UPDATE graphs SET state='archived' WHERE tenant_id=$1 AND id=$2 AND id<>(SELECT active_graph FROM tenants WHERE id=$1)").bind(who.tenant).bind(id).execute(&mut *tx).await?;
    if result.rows_affected() == 0 {
        return Err(Error::conflict("active_or_missing_graph"));
    }
    sqlx::query("UPDATE jobs SET state='cancelled',lease_id=NULL WHERE tenant_id=$1 AND graph_id=$2 AND state IN ('pending','running','failed')").bind(who.tenant).bind(id).execute(&mut *tx).await?;
    db::audit(&mut tx, &who, "graph.archive", Some(id), json!({})).await?;
    tx.commit().await?;
    Ok(Json(json!({"archived":id})))
}
pub async fn retry(
    State(app): State<App>,
    Extension(who): Extension<Identity>,
    Path(id): Path<Uuid>,
) -> Result<Json<Value>> {
    who.require_admin()?;
    let mut tx = db::begin(&app.pool, who.tenant).await?;
    db::lock(&mut tx, who.tenant).await?;
    let count=sqlx::query("UPDATE jobs SET state='pending',attempts=0,error_code=NULL,available_at=now() WHERE tenant_id=$1 AND graph_id=$2 AND state='failed'").bind(who.tenant).bind(id).execute(&mut *tx).await?.rows_affected();
    db::audit(
        &mut tx,
        &who,
        "graph.retry",
        Some(id),
        json!({"jobs":count}),
    )
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"retried":count})))
}
