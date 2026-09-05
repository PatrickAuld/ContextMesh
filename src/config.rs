use serde::Deserialize;
use uuid::Uuid;

#[derive(Clone, Deserialize)]
pub struct Config {
    pub tenants: Vec<Tenant>,
    #[serde(default)]
    pub dev_tokens: Vec<DevToken>,
    #[serde(default = "pool_default")]
    pub pool_size: u32,
}
fn pool_default() -> u32 {
    20
}
#[derive(Clone, Deserialize)]
pub struct Tenant {
    pub id: Uuid,
    pub name: String,
    #[serde(default)]
    pub issuer: Option<String>,
    #[serde(default)]
    pub audience: Option<String>,
    #[serde(default)]
    pub jwks_url: Option<String>,
    #[serde(default = "admin_group")]
    pub admin_group: String,
}
fn admin_group() -> String {
    "contextmesh-admins".into()
}
#[derive(Clone, Deserialize)]
pub struct DevToken {
    pub token: String,
    pub tenant_id: Uuid,
    pub subject: String,
    #[serde(default)]
    pub groups: Vec<String>,
}
impl Config {
    pub fn load(path: &str, dev: bool) -> anyhow::Result<Self> {
        let c: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        anyhow::ensure!(
            c.dev_tokens.is_empty() || dev,
            "dev_tokens require --allow-dev-auth"
        );
        anyhow::ensure!(!c.tenants.is_empty(), "configure at least one tenant");
        let mut ids = std::collections::HashSet::new();
        let mut issuers = std::collections::HashSet::new();
        for t in &c.tenants {
            anyhow::ensure!(ids.insert(t.id), "duplicate tenant");
            if let Some(issuer) = &t.issuer {
                anyhow::ensure!(issuer.starts_with("https://") || dev, "OIDC requires HTTPS");
                anyhow::ensure!(
                    t.audience.is_some() && t.jwks_url.is_some(),
                    "issuer requires audience and jwks_url"
                );
                anyhow::ensure!(
                    t.jwks_url.as_ref().unwrap().starts_with("https://") || dev,
                    "JWKS requires HTTPS"
                );
                anyhow::ensure!(
                    issuers.insert(issuer.clone()),
                    "one issuer must map to one tenant"
                );
            }
        }
        for d in &c.dev_tokens {
            anyhow::ensure!(
                ids.contains(&d.tenant_id) && d.token.len() >= 16,
                "invalid dev credential"
            );
        }
        Ok(c)
    }
}
