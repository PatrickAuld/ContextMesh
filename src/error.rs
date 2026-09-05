use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

#[derive(Debug)]
pub struct Error(pub StatusCode, pub &'static str);
pub type Result<T> = std::result::Result<T, Error>;
impl Error {
    pub fn bad(code: &'static str) -> Self {
        Self(StatusCode::BAD_REQUEST, code)
    }
    pub fn conflict(code: &'static str) -> Self {
        Self(StatusCode::CONFLICT, code)
    }
    pub fn forbidden() -> Self {
        Self(StatusCode::FORBIDDEN, "forbidden")
    }
    pub fn missing() -> Self {
        Self(StatusCode::NOT_FOUND, "not_found")
    }
    pub fn auth() -> Self {
        Self(StatusCode::UNAUTHORIZED, "unauthorized")
    }
}
impl IntoResponse for Error {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}
impl From<sqlx::Error> for Error {
    fn from(e: sqlx::Error) -> Self {
        if let sqlx::Error::Database(ref d) = e {
            if d.code().as_deref() == Some("23505") {
                return Self::conflict("already_exists");
            }
        }
        tracing::error!(kind = "database_error", "database operation failed");
        Self(StatusCode::INTERNAL_SERVER_ERROR, "database_error")
    }
}
