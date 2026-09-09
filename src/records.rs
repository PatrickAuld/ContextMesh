use crate::{
    auth::Identity,
    db,
    domain::{
        AppendRecord, AppendResult, Derivation, NewRecord, Record, Scope, ScopeFilter, Visibility,
    },
    error::{Error, Result},
};
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Row, Transaction};
use std::collections::HashSet;
use std::{future::Future, pin::Pin};
use uuid::Uuid;

const MAX_TRAVERSAL: i64 = 4096;

pub async fn append(
    pool: &PgPool,
    who: &Identity,
    records: Vec<NewRecord>,
    derivation: Option<Derivation>,
    enqueue: bool,
) -> Result<AppendResult> {
    let mut tx = db::begin(pool, who.tenant).await?;
    let result = append_in_transaction(&mut tx, who, records, derivation, enqueue).await?;
    tx.commit().await?;
    Ok(result)
}

pub async fn append_in_transaction(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    mut records: Vec<NewRecord>,
    derivation: Option<Derivation>,
    enqueue: bool,
) -> Result<AppendResult> {
    if records.is_empty() || records.len() > 256 {
        return Err(Error::bad("invalid_batch"));
    }
    if let Some(d) = &derivation {
        d.validate()?;
    }
    db::lock_as(tx, who).await?;
    let mut seen = HashSet::new();
    let mut inserted = HashSet::new();
    let mut result = Vec::with_capacity(records.len());
    let mut changed_supersedes = Vec::new();
    for record in &mut records {
        record.validate()?;
        if !record
            .scope
            .readable_by(&who.subject, &who.subject, &who.groups, who.admin)
        {
            return Err(Error::bad("invalid_scope"));
        }
        if !seen.insert(record.id) {
            return Err(Error::bad("duplicate_record_id"));
        }
        let payload = serde_json::to_string(&json!({
            "author": who.subject, "record": record, "derivation": derivation
        }))
        .map_err(|_| Error::bad("invalid_record"))?;
        let input_hash = db::hash(&payload);
        if let Some(existing) =
            sqlx::query("SELECT input_hash,redacted FROM records WHERE tenant_id=$1 AND id=$2")
                .bind(who.tenant)
                .bind(record.id)
                .fetch_optional(&mut **tx)
                .await?
        {
            if existing.get::<bool, _>("redacted")
                || existing.get::<String, _>("input_hash") != input_hash
            {
                return Err(Error::conflict("record_id_conflict"));
            }
            result.push(AppendRecord {
                id: record.id,
                duplicate: true,
            });
            continue;
        }
        for input in &record.inputs {
            let exists = sqlx::query_scalar::<_, bool>(
                "SELECT EXISTS(SELECT 1 FROM records WHERE tenant_id=$1 AND id=$2)",
            )
            .bind(who.tenant)
            .bind(input)
            .fetch_one(&mut **tx)
            .await?;
            if !exists
                || (!inserted.contains(input) && !is_authorized_available(tx, who, *input).await?)
            {
                return Err(Error::bad("invalid_input"));
            }
        }
        for support in &record.supports {
            let content: String = sqlx::query_scalar(
                "SELECT content FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted",
            )
            .bind(who.tenant)
            .bind(support.record_id)
            .fetch_optional(&mut **tx)
            .await?
            .ok_or_else(|| Error::bad("invalid_support"))?;
            if !content.contains(&support.quote) {
                return Err(Error::bad("invalid_support"));
            }
        }
        for target in &record.supersedes {
            let author: String = sqlx::query_scalar(
                "SELECT author FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted",
            )
            .bind(who.tenant)
            .bind(target)
            .fetch_optional(&mut **tx)
            .await?
            .ok_or_else(|| Error::bad("invalid_supersedes"))?;
            if !who.admin && author != who.subject {
                return Err(Error::forbidden());
            }
        }
        if enqueue && derivation.is_none() {
            let pending: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM jobs WHERE tenant_id=$1 AND state IN ('pending','running')",
            )
            .bind(who.tenant)
            .fetch_one(&mut **tx)
            .await?;
            if pending >= 100_000 {
                return Err(Error::conflict("queue_full"));
            }
        }
        sqlx::query("INSERT INTO records(tenant_id,id,author,agent_id,content,scope,inputs,supports,supersedes,metadata,derivation,input_hash) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)")
            .bind(who.tenant).bind(record.id).bind(&who.subject).bind(who.agent).bind(&record.content)
            .bind(json!(record.scope)).bind(&record.inputs).bind(json!(record.supports)).bind(&record.supersedes)
            .bind(&record.metadata).bind(derivation.as_ref().map(|d| json!(d))).bind(input_hash)
            .execute(&mut **tx).await?;
        inserted.insert(record.id);
        for input in &record.inputs {
            sqlx::query("INSERT INTO record_links(tenant_id,record_id,input_id,supersedes) VALUES($1,$2,$3,$4)")
                .bind(who.tenant).bind(record.id).bind(input).bind(record.supersedes.contains(input))
                .execute(&mut **tx).await?;
        }
        if enqueue && derivation.is_none() {
            sqlx::query("INSERT INTO jobs(tenant_id,id,record_id) VALUES($1,$2,$3)")
                .bind(who.tenant)
                .bind(Uuid::new_v4())
                .bind(record.id)
                .execute(&mut **tx)
                .await?;
        }
        changed_supersedes.extend(record.supersedes.iter().copied());
        result.push(AppendRecord {
            id: record.id,
            duplicate: false,
        });
        db::audit(tx, who, "record.append", Some(record.id), json!({})).await?;
    }
    if !changed_supersedes.is_empty() {
        let mut affected = HashSet::new();
        for id in changed_supersedes {
            affected.extend(descendants(tx, who.tenant, id).await?);
        }
        let affected: Vec<_> = affected.into_iter().collect();
        invalidate_policies(tx, who.tenant, &affected).await?;
        bump_epoch(tx, who.tenant).await?;
    }
    Ok(AppendResult { records: result })
}

pub async fn get(pool: &PgPool, who: &Identity, id: Uuid) -> Result<Record> {
    let mut tx = db::begin(pool, who.tenant).await?;
    let epoch = db::lock_as(&mut tx, who).await?;
    let record = if is_authorized_available(&mut tx, who, id).await? {
        load_record(&mut tx, who.tenant, id).await?
    } else {
        None
    };
    if current_epoch(&mut tx, who.tenant).await? != epoch {
        return Err(Error::forbidden());
    }
    record.ok_or_else(Error::missing)
}

pub async fn authorized_available_by_ids(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    ids: &[Uuid],
    limit: i64,
) -> Result<Vec<Record>> {
    db::lock_as(tx, who).await?;
    if limit <= 0 || limit > MAX_TRAVERSAL || ids.len() as i64 > limit {
        return Err(Error::bad("invalid_limit"));
    }
    let mut out = Vec::new();
    for id in ids {
        if is_authorized_available(tx, who, *id).await? {
            if let Some(record) = load_record(tx, who.tenant, *id).await? {
                out.push(record);
            }
        }
    }
    Ok(out)
}

pub async fn canonical_available(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    task: &str,
    filters: &[ScopeFilter],
    starting: &[Uuid],
    limit: i64,
) -> Result<Vec<Record>> {
    db::lock_as(tx, who).await?;
    if limit <= 0 || limit > MAX_TRAVERSAL || starting.len() as i64 > limit {
        return Err(Error::bad("invalid_limit"));
    }
    for id in starting {
        if !is_authorized_available(tx, who, *id).await? {
            return Err(Error::forbidden());
        }
        let record = load_record(tx, who.tenant, *id)
            .await?
            .ok_or_else(Error::forbidden)?;
        if !is_current(tx, who, &record).await? {
            return Err(Error::forbidden());
        }
    }
    let rows = sqlx::query("SELECT id,sequence FROM records WHERE tenant_id=$1 AND NOT redacted AND search @@ websearch_to_tsquery('simple',$3) AND (jsonb_array_length($4::jsonb)=0 OR EXISTS (SELECT 1 FROM jsonb_to_recordset($4::jsonb) AS f(conversation text,project text) WHERE (f.conversation IS NULL OR scope->>'conversation'=f.conversation) AND (f.project IS NULL OR scope->>'project'=f.project))) ORDER BY ts_rank(search,websearch_to_tsquery('simple',$3)) DESC,sequence DESC LIMIT $2")
        .bind(who.tenant).bind(limit.saturating_mul(4).min(MAX_TRAVERSAL)).bind(task).bind(json!(filters)).fetch_all(&mut **tx).await?;
    let starts: HashSet<_> = starting.iter().copied().collect();
    let mut out = Vec::new();
    for id in starting {
        if let Some(record) = load_record(tx, who.tenant, *id).await? {
            out.push(record);
        }
    }
    for row in rows {
        let id: Uuid = row.get("id");
        if starts.contains(&id) {
            continue;
        }
        let Some(record) = load_record(tx, who.tenant, id).await? else {
            continue;
        };
        let scope_match = filters.is_empty()
            || filters.iter().any(|f| {
                f.conversation
                    .as_ref()
                    .is_none_or(|v| record.scope.conversation.as_ref() == Some(v))
                    && f.project
                        .as_ref()
                        .is_none_or(|v| record.scope.project.as_ref() == Some(v))
            });
        if !scope_match && !starts.contains(&id) {
            continue;
        }
        if is_authorized_available(tx, who, id).await? && is_current(tx, who, &record).await? {
            out.push(record);
            if out.len() as i64 == limit {
                break;
            }
        }
    }
    out.sort_by_key(|r| r.sequence);
    Ok(out)
}

pub async fn record_is_available(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    id: Uuid,
) -> Result<bool> {
    db::lock_as(tx, who).await?;
    is_authorized_available(tx, who, id).await
}

pub async fn record_is_current_available(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    id: Uuid,
) -> Result<bool> {
    db::lock_as(tx, who).await?;
    if !is_authorized_available(tx, who, id).await? {
        return Ok(false);
    }
    let Some(record) = load_record(tx, who.tenant, id).await? else {
        return Ok(false);
    };
    is_current(tx, who, &record).await
}

async fn is_authorized_available(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    id: Uuid,
) -> Result<bool> {
    let rows = sqlx::query(
        "WITH RECURSIVE lineage(id,depth,path) AS (SELECT $2::uuid,0,ARRAY[$2::uuid] UNION ALL SELECT l.input_id,x.depth+1,x.path||l.input_id FROM lineage x JOIN record_links l ON l.tenant_id=$1 AND l.record_id=x.id WHERE x.depth<64 AND NOT l.input_id=ANY(x.path)) SELECT x.id,r.author,r.scope,r.redacted,x.depth FROM lineage x LEFT JOIN records r ON r.tenant_id=$1 AND r.id=x.id LIMIT 4097")
        .bind(who.tenant).bind(id).fetch_all(&mut **tx).await?;
    if rows.is_empty() || rows.len() > MAX_TRAVERSAL as usize {
        return Ok(false);
    }
    for row in rows {
        if row.get::<i32, _>("depth") >= 64 {
            let node: Uuid = row.get("id");
            let more: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM record_links WHERE tenant_id=$1 AND record_id=$2)",
            )
            .bind(who.tenant)
            .bind(node)
            .fetch_one(&mut **tx)
            .await?;
            if more {
                return Ok(false);
            }
        }
        let author: Option<String> = row.try_get("author").ok();
        let scope: Option<Value> = row.try_get("scope").ok();
        if author.is_none() || row.try_get::<bool, _>("redacted").unwrap_or(true) {
            return Ok(false);
        }
        let Ok(scope) = serde_json::from_value::<Scope>(scope.unwrap_or(Value::Null)) else {
            return Ok(false);
        };
        if !scope.readable_by(
            &who.subject,
            author.as_deref().unwrap_or_default(),
            &who.groups,
            who.admin,
        ) {
            return Ok(false);
        }
    }
    Ok(true)
}

async fn is_current(
    tx: &mut Transaction<'_, Postgres>,
    who: &Identity,
    record: &Record,
) -> Result<bool> {
    let mut visiting = HashSet::new();
    let mut budget = MAX_TRAVERSAL as usize;
    current_inner(tx, who, record, &mut visiting, &mut budget).await
}

fn current_inner<'a>(
    tx: &'a mut Transaction<'_, Postgres>,
    who: &'a Identity,
    record: &'a Record,
    visiting: &'a mut HashSet<Uuid>,
    budget: &'a mut usize,
) -> Pin<Box<dyn Future<Output = Result<bool>> + Send + 'a>> {
    Box::pin(async move {
        if *budget == 0 || visiting.len() >= 64 || !visiting.insert(record.id) {
            return Err(Error::bad("lineage_too_large"));
        }
        *budget -= 1;
        let outcome = async {
        let ancestor_rows = sqlx::query("WITH RECURSIVE a(id,path) AS (SELECT input_id,ARRAY[record_id,input_id] FROM record_links WHERE tenant_id=$1 AND record_id=$2 AND NOT supersedes UNION ALL SELECT l.input_id,a.path||l.input_id FROM a JOIN record_links l ON l.tenant_id=$1 AND l.record_id=a.id AND NOT l.supersedes WHERE cardinality(a.path)<65 AND NOT l.input_id=ANY(a.path)) SELECT DISTINCT id,cardinality(path) AS depth FROM a LIMIT 4097")
        .bind(who.tenant).bind(record.id).fetch_all(&mut **tx).await?;
        if ancestor_rows.len() > MAX_TRAVERSAL as usize {
            return Ok(false);
        }
        let mut ancestors = Vec::with_capacity(ancestor_rows.len());
        for row in ancestor_rows {
            let node: Uuid = row.get("id");
            if row.get::<i32, _>("depth") >= 65 {
                let more: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM record_links WHERE tenant_id=$1 AND record_id=$2)",
                )
                .bind(who.tenant)
                .bind(node)
                .fetch_one(&mut **tx)
                .await?;
                if more {
                    return Ok(false);
                }
            }
            ancestors.push(node);
        }
        for ancestor in ancestors {
            let successors = sqlx::query_scalar::<_, Uuid>(
            "SELECT record_id FROM record_links WHERE tenant_id=$1 AND input_id=$2 AND supersedes",
        )
        .bind(who.tenant)
        .bind(ancestor)
        .fetch_all(&mut **tx)
        .await?;
            for successor in successors {
                if successor != record.id && is_authorized_available(tx, who, successor).await? {
                    if let Some(next) = load_record(tx, who.tenant, successor).await? {
                        if current_inner(tx, who, &next, visiting, budget).await? {
                            return Ok(false);
                        }
                    }
                }
            }
        }
        let direct_successors = sqlx::query_scalar::<_, Uuid>(
            "SELECT record_id FROM record_links WHERE tenant_id=$1 AND input_id=$2 AND supersedes",
        )
        .bind(who.tenant)
        .bind(record.id)
        .fetch_all(&mut **tx)
        .await?;
        for successor in direct_successors {
            if is_authorized_available(tx, who, successor).await? {
                if let Some(next) = load_record(tx, who.tenant, successor).await? {
                    if current_inner(tx, who, &next, visiting, budget).await? {
                        return Ok(false);
                    }
                }
            }
        }
        Ok(true)
        }.await;
        visiting.remove(&record.id);
        outcome
    })
}

async fn load_record(
    tx: &mut Transaction<'_, Postgres>,
    tenant: Uuid,
    id: Uuid,
) -> Result<Option<Record>> {
    let Some(row) = sqlx::query("SELECT id,content,scope,inputs,supports,supersedes,metadata,author,agent_id,recorded_at,sequence,derivation FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted")
        .bind(tenant).bind(id).fetch_optional(&mut **tx).await? else { return Ok(None) };
    let scope = serde_json::from_value(row.get("scope"))
        .map_err(|_| Error::bad("invalid_stored_record"))?;
    let supports = serde_json::from_value(row.get("supports"))
        .map_err(|_| Error::bad("invalid_stored_record"))?;
    let derivation: Option<Value> = row.get("derivation");
    let derivation = derivation
        .map(serde_json::from_value)
        .transpose()
        .map_err(|_| Error::bad("invalid_stored_record"))?;
    Ok(Some(Record {
        id: row.get("id"),
        content: row.get("content"),
        scope,
        inputs: row.get("inputs"),
        supports,
        supersedes: row.get("supersedes"),
        metadata: row.get("metadata"),
        author: row.get("author"),
        agent_id: row.get("agent_id"),
        recorded_at: row.get("recorded_at"),
        sequence: row.get("sequence"),
        derivation,
    }))
}

pub async fn redact(pool: &PgPool, who: &Identity, id: Uuid) -> Result<()> {
    who.require_admin()?;
    let mut tx = db::begin(pool, who.tenant).await?;
    db::lock_as(&mut tx, who).await?;
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted)",
    )
    .bind(who.tenant)
    .bind(id)
    .fetch_one(&mut *tx)
    .await?;
    if !exists {
        return Err(Error::missing());
    }
    let affected = descendants(&mut tx, who.tenant, id).await?;
    if affected.is_empty() {
        return Err(Error::missing());
    }
    sqlx::query("UPDATE records SET redacted=true,content='',supports='[]',metadata='{}',derivation=NULL,input_hash='' WHERE tenant_id=$1 AND id=ANY($2)")
        .bind(who.tenant)
        .bind(&affected)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE jobs SET state='cancelled',error_code='source_unavailable',lease_id=NULL,lease_until=NULL WHERE tenant_id=$1 AND record_id=ANY($2) AND state IN ('pending','running')")
        .bind(who.tenant)
        .bind(&affected)
        .execute(&mut *tx)
        .await?;
    invalidate_policies(&mut tx, who.tenant, &affected).await?;
    bump_epoch(&mut tx, who.tenant).await?;
    db::audit(
        &mut tx,
        who,
        "record.redact",
        Some(id),
        json!({"affected": affected.len()}),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn classify(
    pool: &PgPool,
    who: &Identity,
    id: Uuid,
    visibility: Visibility,
    mut groups: Vec<String>,
) -> Result<()> {
    who.require_admin()?;
    if matches!(visibility, Visibility::Personal) {
        groups.clear();
    }
    let mut tx = db::begin(pool, who.tenant).await?;
    db::lock_as(&mut tx, who).await?;
    let value: Value = sqlx::query_scalar(
        "SELECT scope FROM records WHERE tenant_id=$1 AND id=$2 AND NOT redacted",
    )
    .bind(who.tenant)
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or_else(Error::missing)?;
    let mut scope: Scope =
        serde_json::from_value(value).map_err(|_| Error::bad("invalid_stored_record"))?;
    scope.visibility = visibility;
    scope.groups = groups;
    scope.validate()?;
    let affected = descendants(&mut tx, who.tenant, id).await?;
    sqlx::query("UPDATE records SET scope=$3 WHERE tenant_id=$1 AND id=$2")
        .bind(who.tenant)
        .bind(id)
        .bind(json!(scope))
        .execute(&mut *tx)
        .await?;
    invalidate_policies(&mut tx, who.tenant, &affected).await?;
    bump_epoch(&mut tx, who.tenant).await?;
    db::audit(
        &mut tx,
        who,
        "record.classify",
        Some(id),
        json!({"affected": affected.len()}),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn descendants(
    tx: &mut Transaction<'_, Postgres>,
    tenant: Uuid,
    id: Uuid,
) -> Result<Vec<Uuid>> {
    let rows = sqlx::query("WITH RECURSIVE d(id,path) AS (SELECT $2::uuid,ARRAY[$2::uuid] UNION ALL SELECT l.record_id,d.path||l.record_id FROM d JOIN record_links l ON l.tenant_id=$1 AND l.input_id=d.id WHERE cardinality(d.path)<65 AND NOT l.record_id=ANY(d.path)) SELECT DISTINCT id,cardinality(path) AS depth FROM d LIMIT 4097")
        .bind(tenant).bind(id).fetch_all(&mut **tx).await?;
    if rows.len() > MAX_TRAVERSAL as usize {
        return Err(Error::bad("lineage_too_large"));
    }
    let mut ids = Vec::with_capacity(rows.len());
    for row in rows {
        let node: Uuid = row.get("id");
        if row.get::<i32, _>("depth") >= 65 {
            let more: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM record_links WHERE tenant_id=$1 AND input_id=$2)",
            )
            .bind(tenant)
            .bind(node)
            .fetch_one(&mut **tx)
            .await?;
            if more {
                return Err(Error::bad("lineage_too_large"));
            }
        }
        ids.push(node);
    }
    Ok(ids)
}

async fn invalidate_policies(
    tx: &mut Transaction<'_, Postgres>,
    tenant: Uuid,
    ids: &[Uuid],
) -> Result<()> {
    sqlx::query("UPDATE policies SET enabled=false WHERE tenant_id=$1 AND evidence && $2")
        .bind(tenant)
        .bind(ids)
        .execute(&mut **tx)
        .await?;
    Ok(())
}
async fn bump_epoch(tx: &mut Transaction<'_, Postgres>, tenant: Uuid) -> Result<()> {
    sqlx::query("UPDATE tenants SET security_epoch=security_epoch+1 WHERE id=$1")
        .bind(tenant)
        .execute(&mut **tx)
        .await?;
    Ok(())
}
pub async fn current_epoch(tx: &mut Transaction<'_, Postgres>, tenant: Uuid) -> Result<i64> {
    Ok(
        sqlx::query_scalar("SELECT security_epoch FROM tenants WHERE id=$1")
            .bind(tenant)
            .fetch_one(&mut **tx)
            .await?,
    )
}
