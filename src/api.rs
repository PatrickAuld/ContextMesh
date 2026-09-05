use crate::{
    auth::{Auth, Identity},
    config::Config,
    db,
    domain::GraphConfig,
    error::Error,
    events, graphs,
    inference::Gateway,
    ops, policy, query,
};
use axum::{
    extract::{DefaultBodyLimit, Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::Response,
    routing::{get, post},
    Json, Router,
};
use serde_json::json;
use sqlx::{postgres::PgPoolOptions, PgPool};
use std::sync::Arc;
use tokio::sync::Semaphore;
use uuid::Uuid;

#[derive(Clone)]
pub struct App {
    pub pool: PgPool,
    pub config: Arc<Config>,
    pub auth: Auth,
    pub gateway: Gateway,
    pub queries: Arc<Semaphore>,
}
impl App {
    pub async fn connect(database: &str, config: Config) -> anyhow::Result<Self> {
        let pool = PgPoolOptions::new()
            .max_connections(config.pool_size)
            .acquire_timeout(std::time::Duration::from_secs(10))
            .connect(database)
            .await?;
        let unsafe_role: bool = sqlx::query_scalar(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user",
        )
        .fetch_one(&pool)
        .await?;
        anyhow::ensure!(
            !unsafe_role,
            "runtime database role must not be superuser or BYPASSRLS"
        );
        let config = Arc::new(config);
        Ok(Self {
            pool,
            auth: Auth::new(config.clone()),
            config,
            gateway: Gateway::from_env()?,
            queries: Arc::new(Semaphore::new(32)),
        })
    }
    pub async fn bootstrap(&self) -> anyhow::Result<()> {
        for tenant in &self.config.tenants {
            let mut tx = db::begin(&self.pool, tenant.id)
                .await
                .map_err(|_| anyhow::anyhow!("database_begin"))?;
            sqlx::query("INSERT INTO tenants(id,name) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name").bind(tenant.id).bind(&tenant.name).execute(&mut *tx).await?;
            db::lock(&mut tx, tenant.id)
                .await
                .map_err(|_| anyhow::anyhow!("tenant_lock"))?;
            let active: Option<Uuid> =
                sqlx::query_scalar("SELECT active_graph FROM tenants WHERE id=$1")
                    .bind(tenant.id)
                    .fetch_one(&mut *tx)
                    .await?;
            if active.is_none() {
                let id = Uuid::new_v4();
                let mut config = GraphConfig::default();
                if self.gateway.configured() {
                    config.mode = "llm".into();
                    config.model = self.gateway.model();
                }
                sqlx::query("INSERT INTO graphs(tenant_id,id,name,config,state) VALUES($1,$2,'initial',$3,'ready')").bind(tenant.id).bind(id).bind(serde_json::to_value(config)?).execute(&mut *tx).await?;
                sqlx::query("UPDATE tenants SET active_graph=$2 WHERE id=$1")
                    .bind(tenant.id)
                    .bind(id)
                    .execute(&mut *tx)
                    .await?;
            }
            tx.commit().await?;
        }
        Ok(())
    }
}
async fn auth(State(app): State<App>, mut request: Request, next: Next) -> Result<Response, Error> {
    let token = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|t| t.len() <= 16384)
        .ok_or_else(Error::auth)?;
    let identity = app.auth.authenticate(&app.pool, token).await?;
    request.extensions_mut().insert(identity);
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-store"),
    );
    response.headers_mut().insert(
        "x-content-type-options",
        axum::http::HeaderValue::from_static("nosniff"),
    );
    Ok(response)
}
async fn query_limit(
    State(app): State<App>,
    request: Request,
    next: Next,
) -> Result<Response, Error> {
    let _permit = app
        .queries
        .try_acquire()
        .map_err(|_| Error(StatusCode::TOO_MANY_REQUESTS, "query_capacity"))?;
    Ok(next.run(request).await)
}
pub fn router(app: App) -> Router {
    let queries = Router::new()
        .route("/v1/query", post(query::query))
        .route("/v1/wiki", post(query::wiki))
        .layer(middleware::from_fn_with_state(app.clone(), query_limit));
    let protected = Router::new()
        .route(
            "/v1/identity",
            get(|axum::Extension(who): axum::Extension<Identity>| async move { Json(who) }),
        )
        .route("/v1/events", get(events::list).post(events::insert))
        .route("/v1/events/{id}", get(events::get))
        .route("/v1/events/{id}/redact", post(events::redact))
        .route("/v1/events/{id}/classification", post(events::classify))
        .route("/v1/graphs", get(graphs::list).post(graphs::create))
        .route("/v1/graphs/{id}/promote", post(graphs::promote))
        .route("/v1/graphs/{id}/edges", get(graphs::edges))
        .route("/v1/graphs/{id}/archive", post(graphs::archive))
        .route("/v1/graphs/{id}/retry", post(graphs::retry))
        .route("/v1/policies", get(policy::list).post(policy::create))
        .route("/v1/policies/{id}/revoke", post(policy::revoke))
        .route("/v1/agents", post(ops::delegate))
        .route("/v1/agents/{id}/revoke", post(ops::revoke_agent))
        .route("/v1/principals/status", post(ops::principal))
        .route("/v1/audit", get(ops::audit))
        .route("/v1/lineage/{id}", get(ops::lineage))
        .route("/v1/status", get(ops::status))
        .route("/v1/metrics", get(ops::metrics))
        .route("/v1/redactions", get(ops::redactions))
        .route("/v1/jobs", get(ops::jobs))
        .route("/v1/receipts/{id}", get(query::receipt))
        .merge(queries)
        .layer(middleware::from_fn_with_state(app.clone(), auth));
    Router::new()
        .route("/health", get(|| async { Json(json!({"status":"ok"})) }))
        .route(
            "/ready",
            get(|State(app): State<App>| async move {
                match sqlx::query("SELECT 1").execute(&app.pool).await {
                    Ok(_) => StatusCode::OK,
                    Err(_) => StatusCode::SERVICE_UNAVAILABLE,
                }
            }),
        )
        .merge(protected)
        .layer(DefaultBodyLimit::max(128 * 1024))
        .with_state(app)
}
