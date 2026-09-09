use crate::{
    auth::{Auth, Identity},
    config::Config,
    db,
    error::Error,
    inference::Gateway,
    ops, policy, query, records,
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
        .route("/v1/context", post(query::context))
        .layer(middleware::from_fn_with_state(app.clone(), query_limit));
    let protected = Router::new()
        .route(
            "/v1/identity",
            get(|axum::Extension(who): axum::Extension<Identity>| async move { Json(who) }),
        )
        .route("/v1/records", post(append_records))
        .route("/v1/records/{id}", get(get_record))
        .route("/v1/records/{id}/redact", post(redact_record))
        .route("/v1/records/{id}/classification", post(classify_record))
        .route("/v1/policies", get(policy::list).post(policy::create))
        .route("/v1/policies/{id}/revoke", post(policy::revoke))
        .route("/v1/agents", post(ops::delegate))
        .route("/v1/agents/{id}/revoke", post(ops::revoke_agent))
        .route("/v1/principals/status", post(ops::principal))
        .route("/v1/audit", get(ops::audit))
        .route("/v1/status", get(ops::status))
        .route("/v1/metrics", get(ops::metrics))
        .route("/v1/redactions", get(ops::redactions))
        .route("/v1/jobs", get(ops::jobs))
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
        .layer(DefaultBodyLimit::max(2 * 1024 * 1024))
        .with_state(app)
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct AppendRecords {
    records: Vec<crate::domain::NewRecord>,
}
async fn append_records(
    State(app): State<App>,
    axum::Extension(who): axum::Extension<Identity>,
    Json(input): Json<AppendRecords>,
) -> Result<Json<crate::domain::AppendResult>, Error> {
    records::append(
        &app.pool,
        &who,
        input.records,
        None,
        app.gateway.configured(),
    )
    .await
    .map(Json)
}
async fn get_record(
    State(app): State<App>,
    axum::Extension(who): axum::Extension<Identity>,
    axum::extract::Path(id): axum::extract::Path<uuid::Uuid>,
) -> Result<Json<crate::domain::Record>, Error> {
    records::get(&app.pool, &who, id).await.map(Json)
}
async fn redact_record(
    State(app): State<App>,
    axum::Extension(who): axum::Extension<Identity>,
    axum::extract::Path(id): axum::extract::Path<uuid::Uuid>,
) -> Result<Json<serde_json::Value>, Error> {
    records::redact(&app.pool, &who, id).await?;
    Ok(Json(json!({"redacted": id})))
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Classification {
    visibility: crate::domain::Visibility,
    #[serde(default)]
    groups: Vec<String>,
}
async fn classify_record(
    State(app): State<App>,
    axum::Extension(who): axum::Extension<Identity>,
    axum::extract::Path(id): axum::extract::Path<uuid::Uuid>,
    Json(input): Json<Classification>,
) -> Result<Json<serde_json::Value>, Error> {
    records::classify(&app.pool, &who, id, input.visibility, input.groups).await?;
    Ok(Json(json!({"updated": id})))
}
