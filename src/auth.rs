use crate::{
    config::Config,
    db,
    error::{Error, Result},
};
use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::RwLock;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize)]
pub struct Identity {
    pub tenant: Uuid,
    pub subject: String,
    pub groups: Vec<String>,
    pub admin: bool,
    pub agent: Option<Uuid>,
}
impl Identity {
    pub fn require_admin(&self) -> Result<()> {
        if self.admin && self.agent.is_none() {
            Ok(())
        } else {
            Err(Error::forbidden())
        }
    }
    pub fn can_read(&self, classification: &str, groups: &[String]) -> bool {
        classification == "internal" || self.admin || groups.iter().any(|g| self.groups.contains(g))
    }
}
#[derive(Clone)]
pub struct Auth {
    config: Arc<Config>,
    http: reqwest::Client,
    keys: Arc<RwLock<HashMap<String, (Instant, JwkSet)>>>,
}
#[derive(Deserialize)]
struct Claims {
    sub: String,
    #[serde(default)]
    groups: Vec<String>,
}
impl Auth {
    pub fn new(config: Arc<Config>) -> Self {
        Self {
            config,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .unwrap(),
            keys: Default::default(),
        }
    }
    pub async fn authenticate(&self, pool: &PgPool, token: &str) -> Result<Identity> {
        if token.starts_with("cm_") {
            return self.agent(pool, token).await;
        }
        for d in &self.config.dev_tokens {
            if db::hash(token) == db::hash(&d.token) {
                return self
                    .human(pool, d.tenant_id, d.subject.clone(), d.groups.clone())
                    .await;
            }
        }
        let header = decode_header(token).map_err(|_| Error::auth())?;
        if header.alg != Algorithm::RS256 {
            return Err(Error::auth());
        }
        let kid = header.kid.ok_or_else(Error::auth)?;
        for t in &self.config.tenants {
            let (Some(issuer), Some(audience), Some(url)) = (&t.issuer, &t.audience, &t.jwks_url)
            else {
                continue;
            };
            let cached = self.keys.read().await.get(url).cloned();
            let keys = match cached {
                Some((at, keys)) if at.elapsed() < Duration::from_secs(300) => keys,
                _ => {
                    let response = self.http.get(url).send().await.map_err(|_| Error::auth())?;
                    if !response.status().is_success()
                        || response.content_length().unwrap_or(0) > 1_000_000
                    {
                        return Err(Error::auth());
                    }
                    let keys: JwkSet = response.json().await.map_err(|_| Error::auth())?;
                    self.keys
                        .write()
                        .await
                        .insert(url.clone(), (Instant::now(), keys.clone()));
                    keys
                }
            };
            let Some(jwk) = keys.find(&kid) else { continue };
            let key = DecodingKey::from_jwk(jwk).map_err(|_| Error::auth())?;
            let mut validation = Validation::new(Algorithm::RS256);
            validation.set_issuer(&[issuer]);
            validation.set_audience(&[audience]);
            validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
            validation.validate_nbf = true;
            validation.leeway = 15;
            if let Ok(data) = decode::<Claims>(token, &key, &validation) {
                if data.claims.sub.is_empty() {
                    return Err(Error::auth());
                }
                return self
                    .human(pool, t.id, data.claims.sub, data.claims.groups)
                    .await;
            }
        }
        Err(Error::auth())
    }
    async fn human(
        &self,
        pool: &PgPool,
        tenant: Uuid,
        subject: String,
        mut groups: Vec<String>,
    ) -> Result<Identity> {
        groups.sort();
        groups.dedup();
        let mut tx = db::begin(pool, tenant).await?;
        db::lock(&mut tx, tenant).await?;
        let old =
            sqlx::query("SELECT groups,disabled FROM principals WHERE tenant_id=$1 AND subject=$2")
                .bind(tenant)
                .bind(&subject)
                .fetch_optional(&mut *tx)
                .await?;
        if old.as_ref().is_some_and(|r| r.get::<bool, _>("disabled")) {
            return Err(Error::forbidden());
        }
        if old
            .as_ref()
            .is_some_and(|r| r.get::<Vec<String>, _>("groups") != groups)
        {
            sqlx::query("UPDATE tenants SET security_epoch=security_epoch+1 WHERE id=$1")
                .bind(tenant)
                .execute(&mut *tx)
                .await?;
        }
        sqlx::query("INSERT INTO principals(tenant_id,subject,groups) VALUES($1,$2,$3) ON CONFLICT(tenant_id,subject) DO UPDATE SET groups=EXCLUDED.groups,updated_at=now()")
            .bind(tenant).bind(&subject).bind(&groups).execute(&mut *tx).await?;
        tx.commit().await?;
        let admin_group = &self
            .config
            .tenants
            .iter()
            .find(|t| t.id == tenant)
            .ok_or_else(Error::auth)?
            .admin_group;
        Ok(Identity {
            tenant,
            subject,
            admin: groups.contains(admin_group),
            groups,
            agent: None,
        })
    }
    async fn agent(&self, pool: &PgPool, token: &str) -> Result<Identity> {
        let tenant = token
            .split('_')
            .nth(1)
            .and_then(|s| Uuid::parse_str(s).ok())
            .ok_or_else(Error::auth)?;
        let mut tx = db::begin(pool, tenant).await?;
        let row=sqlx::query("SELECT a.id,a.owner,p.groups FROM agent_tokens a JOIN principals p ON p.tenant_id=a.tenant_id AND p.subject=a.owner WHERE a.tenant_id=$1 AND a.token_hash=$2 AND NOT a.revoked AND a.expires_at>now() AND NOT p.disabled")
            .bind(tenant).bind(db::hash(token)).fetch_optional(&mut *tx).await?.ok_or_else(Error::auth)?;
        Ok(Identity {
            tenant,
            subject: row.get("owner"),
            groups: row.get("groups"),
            admin: false,
            agent: Some(row.get("id")),
        })
    }
}
