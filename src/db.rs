use crate::{auth::Identity, error::Result};
use serde_json::Value;
use sqlx::{PgPool, Postgres, Transaction};
use uuid::Uuid;

pub async fn begin(pool: &PgPool, tenant: Uuid) -> Result<Transaction<'_, Postgres>> {
    let mut tx = pool.begin().await?;
    sqlx::query("SELECT set_config('app.tenant_id',$1,true)")
        .bind(tenant.to_string())
        .execute(&mut *tx)
        .await?;
    Ok(tx)
}
pub async fn lock(tx: &mut Transaction<'_, Postgres>, tenant: Uuid) -> Result<i64> {
    Ok(
        sqlx::query_scalar("SELECT security_epoch FROM tenants WHERE id=$1 FOR UPDATE")
            .bind(tenant)
            .fetch_one(&mut **tx)
            .await?,
    )
}
pub async fn audit(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    action: &str,
    target: Option<Uuid>,
    metadata: Value,
) -> Result<()> {
    sqlx::query("INSERT INTO audit(tenant_id,actor,agent_id,action,target,metadata) VALUES($1,$2,$3,$4,$5,$6)")
        .bind(who.tenant).bind(&who.subject).bind(who.agent).bind(action).bind(target).bind(metadata).execute(&mut **tx).await?;
    Ok(())
}
pub fn hash(value: &str) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
