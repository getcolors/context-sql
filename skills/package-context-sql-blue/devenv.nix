{ pkgs, ... }:
{
  packages = [ pkgs.postgresql_18 pkgs.python311 pkgs.uv pkgs.git ];
}
