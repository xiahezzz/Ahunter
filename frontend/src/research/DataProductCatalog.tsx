import { Database } from "lucide-react";
import { useState } from "react";
import { ProductGroup } from "./contracts";

export default function DataProductCatalog({ products, loading, error, onRefresh }: { products: ProductGroup[]; loading: boolean; error: string | null; onRefresh: () => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  return <section className="panel data-product-catalog" aria-label="研究数据源">
    <div className="research-panel-heading"><h2><Database size={18} />研究数据源</h2><button type="button" className="secondary-button" onClick={onRefresh} disabled={loading}>{loading ? "正在刷新" : "刷新目录"}</button></div>
    {loading && products.length === 0 ? <p className="unavailable-message">正在读取研究数据源…</p> : null}
    {error && products.length === 0 ? <p className="unavailable-message" role="alert">{error}</p> : null}
    {products.length === 0 && !loading && !error ? <p className="empty">暂无已发布数据源</p> : null}
    <div className="data-product-cards">
      {products.map((group) => {
        const history = group.history.filter((item) => item.product_ref !== group.latest.product_ref);
        const open = expanded.has(group.product_id);
        return <article className="data-product-card" key={group.product_id} aria-label={`${group.latest.title} ${group.latest.product_ref}`}>
          <div><strong>{group.latest.title}</strong><small>{group.latest.product_ref}</small></div>
          <dl>
            <div><dt>数据来源</dt><dd>{group.latest.providers.map((provider) => provider.display_name).join("、") || "内部计算生成"}</dd></div>
            <div><dt>构建依赖</dt><dd>{group.latest.dependencies.length ? group.latest.dependencies.join("、") : "无"}</dd></div>
          </dl>
          {group.latest.dependencies.length ? <p className="research-note">构建需要这些依赖，Agent 不会自动获得它们的可见权限。</p> : null}
          {group.latest.supports_feed_scope ? <p className="research-note">此产品需要明确逐项选择 MX RID Feeds。</p> : null}
          {history.length ? <><button type="button" className="text-button" onClick={() => setExpanded((current) => { const next = new Set(current); if (next.has(group.product_id)) next.delete(group.product_id); else next.add(group.product_id); return next; })}>{open ? "收起历史版本" : "查看历史版本"}</button>{open ? <ul className="research-history-list">{history.map((product) => <li key={product.product_ref}>{product.product_ref} · {product.title}</li>)}</ul> : null}</> : null}
        </article>;
      })}
    </div>
  </section>;
}
