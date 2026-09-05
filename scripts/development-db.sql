CREATE ROLE cm_owner LOGIN PASSWORD 'local-owner-password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE cm_runtime LOGIN PASSWORD 'local-runtime-password' NOSUPERUSER NOBYPASSRLS;
ALTER DATABASE contextmesh OWNER TO cm_owner;
ALTER SCHEMA public OWNER TO cm_owner;
