use crate::{api::App, auth::Identity, db, domain::GraphConfig, error::Result};
use serde_json::{json, Value};
use sqlx::Row;
use uuid::Uuid;

pub async fn run(app: App, mut stop: tokio::sync::watch::Receiver<bool>) {
    let mut turn = 0usize;
    loop {
        if *stop.borrow() {
            break;
        }
        let tenants = &app.config.tenants;
        let mut worked = false;
        for offset in 0..tenants.len() {
            let tenant = tenants[(turn + offset) % tenants.len()].id;
            match process(&app, tenant).await {
                Ok(true) => {
                    worked = true;
                    break;
                }
                Ok(false) => {}
                Err(_) => tracing::error!(%tenant,"worker database operation failed"),
            }
        }
        turn = (turn + 1) % tenants.len();
        if !worked {
            tokio::select! {_=tokio::time::sleep(std::time::Duration::from_millis(250))=>{},_=stop.changed()=>{}}
        }
    }
}
async fn process(app: &App, tenant: Uuid) -> Result<bool> {
    let mut tx = db::begin(&app.pool, tenant).await?;
    backfill(&mut tx, tenant).await?;
    sqlx::query("UPDATE graphs SET state='ready' WHERE tenant_id=$1 AND state='building' AND backfill_cursor>=backfill_target AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.tenant_id=graphs.tenant_id AND jobs.graph_id=graphs.id AND state IN ('pending','running','failed'))").bind(tenant).execute(&mut *tx).await?;
    tx.commit().await?;
    let mut tx = db::begin(&app.pool, tenant).await?;
    sqlx::query("UPDATE jobs SET state='failed',error_code='lease_exhausted' WHERE tenant_id=$1 AND state='running' AND lease_until<now() AND attempts>=5").bind(tenant).execute(&mut *tx).await?;
    let lease = Uuid::new_v4();
    let row=sqlx::query("UPDATE jobs SET state='running',attempts=attempts+1,lease_id=$2,lease_until=now()+interval '60 seconds' WHERE tenant_id=$1 AND id=(SELECT id FROM jobs WHERE tenant_id=$1 AND attempts<5 AND ((state='pending' AND available_at<=now()) OR (state='running' AND lease_until<now())) ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,graph_id,event_id")
        .bind(tenant).bind(lease).fetch_optional(&mut *tx).await?;
    let Some(row) = row else {
        tx.commit().await?;
        return Ok(false);
    };
    let job: Uuid = row.get("id");
    let graph: Uuid = row.get("graph_id");
    let event: Uuid = row.get("event_id");
    let input=sqlx::query("SELECT e.body,e.context,g.config FROM events e JOIN graphs g ON g.tenant_id=e.tenant_id AND g.id=$3 WHERE e.tenant_id=$1 AND e.id=$2 AND e.current AND NOT e.redacted AND g.state<>'archived'")
        .bind(tenant).bind(event).bind(graph).fetch_optional(&mut *tx).await?;
    tx.commit().await?;
    let Some(input) = input else {
        let mut tx = db::begin(&app.pool, tenant).await?;
        sqlx::query(
            "UPDATE jobs SET state='cancelled' WHERE tenant_id=$1 AND id=$2 AND lease_id=$3",
        )
        .bind(tenant)
        .bind(job)
        .bind(lease)
        .execute(&mut *tx)
        .await?;
        mark_ready(&mut tx, tenant, graph).await?;
        tx.commit().await?;
        return Ok(true);
    };
    let body: String = input.get("body");
    let context: Value = input.get("context");
    let config: GraphConfig =
        serde_json::from_value(input.get("config")).expect("validated graph config");
    let result = app.gateway.curate(&body, &context, &config).await;
    let mut tx = db::begin(&app.pool, tenant).await?;
    db::lock(&mut tx, tenant).await?;
    let valid:Option<Uuid>=sqlx::query_scalar("SELECT j.id FROM jobs j JOIN events e ON e.tenant_id=j.tenant_id AND e.id=j.event_id JOIN graphs g ON g.tenant_id=j.tenant_id AND g.id=j.graph_id WHERE j.tenant_id=$1 AND j.id=$2 AND j.lease_id=$3 AND j.state='running' AND j.lease_until>now() AND e.current AND NOT e.redacted AND g.state<>'archived' FOR UPDATE OF j")
        .bind(tenant).bind(job).bind(lease).fetch_optional(&mut *tx).await?;
    if valid.is_none() {
        return Ok(true);
    }
    let result = result.and_then(|claims| {
        for c in &claims {
            c.validate(&body)
                .map_err(|_| anyhow::anyhow!("invalid_claim"))?;
        }
        Ok(claims)
    });
    match result {
        Ok(claims) => {
            sqlx::query("DELETE FROM claims WHERE tenant_id=$1 AND graph_id=$2 AND event_id=$3")
                .bind(tenant)
                .bind(graph)
                .bind(event)
                .execute(&mut *tx)
                .await?;
            for c in &claims {
                let id = Uuid::new_v4();
                sqlx::query("INSERT INTO claims(tenant_id,graph_id,id,event_id,body,quote,intent,entities,applies,dependencies,slot) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)")
                    .bind(tenant).bind(graph).bind(id).bind(event).bind(&c.text).bind(&c.quote).bind(&c.intent).bind(&c.entities).bind(serde_json::to_value(&c.applies).unwrap()).bind(serde_json::to_value(&c.dependencies).unwrap()).bind(&c.slot).execute(&mut *tx).await?;
                for edge in &c.relations {
                    sqlx::query("INSERT INTO edges(tenant_id,graph_id,claim_id,from_entity,relation,to_entity) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING")
                        .bind(tenant).bind(graph).bind(id).bind(&edge.from).bind(&edge.relation).bind(&edge.to).execute(&mut *tx).await?;
                }
            }
            sqlx::query("UPDATE jobs SET state='done',error_code=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3").bind(tenant).bind(job).bind(lease).execute(&mut *tx).await?;
            let who = Identity {
                tenant,
                subject: "system:curator".into(),
                groups: vec![],
                admin: false,
                agent: None,
            };
            db::audit(
                &mut tx,
                &who,
                "graph.curate",
                Some(event),
                json!({"graph_id":graph,"job_id":job,"claim_count":claims.len()}),
            )
            .await?;
        }
        Err(_) => {
            sqlx::query("UPDATE jobs SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,error_code='curation_failed',available_at=now()+make_interval(secs=>least(60,power(2,attempts)::int)),lease_until=NULL WHERE tenant_id=$1 AND id=$2 AND lease_id=$3")
                .bind(tenant).bind(job).bind(lease).execute(&mut *tx).await?;
            tracing::warn!(%tenant,%job,"curation failed; retry state persisted");
        }
    }
    mark_ready(&mut tx, tenant, graph).await?;
    tx.commit().await?;
    Ok(true)
}
async fn mark_ready(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    tenant: Uuid,
    graph: Uuid,
) -> Result<()> {
    sqlx::query("UPDATE graphs SET state='ready' WHERE tenant_id=$1 AND id=$2 AND state='building' AND backfill_cursor>=backfill_target AND NOT EXISTS(SELECT 1 FROM jobs WHERE tenant_id=$1 AND graph_id=$2 AND state IN ('pending','running','failed'))")
        .bind(tenant).bind(graph).execute(&mut **tx).await?;
    Ok(())
}

async fn backfill(tx: &mut sqlx::Transaction<'_, sqlx::Postgres>, tenant: Uuid) -> Result<()> {
    let graph=sqlx::query("SELECT id,backfill_cursor,backfill_target FROM graphs WHERE tenant_id=$1 AND state='building' AND backfill_cursor<backfill_target ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").bind(tenant).fetch_optional(&mut **tx).await?;
    if let Some(graph) = graph {
        let id: Uuid = graph.get("id");
        let target: i64 = graph.get("backfill_target");
        let rows=sqlx::query("SELECT id,sequence FROM events WHERE tenant_id=$1 AND sequence>$2 AND sequence<=$3 AND current AND NOT redacted ORDER BY sequence LIMIT 500").bind(tenant).bind(graph.get::<i64,_>("backfill_cursor")).bind(target).fetch_all(&mut **tx).await?;
        let ids: Vec<Uuid> = rows.iter().map(|r| r.get("id")).collect();
        sqlx::query("INSERT INTO jobs(tenant_id,id,graph_id,event_id) SELECT $1,gen_random_uuid(),$2,unnest($3::uuid[]) ON CONFLICT(tenant_id,graph_id,event_id) DO NOTHING").bind(tenant).bind(id).bind(&ids).execute(&mut **tx).await?;
        let cursor = if rows.len() < 500 {
            target
        } else {
            rows.last().unwrap().get::<i64, _>("sequence")
        };
        sqlx::query("UPDATE graphs SET backfill_cursor=$3 WHERE tenant_id=$1 AND id=$2")
            .bind(tenant)
            .bind(id)
            .bind(cursor)
            .execute(&mut **tx)
            .await?;
    }
    Ok(())
}
