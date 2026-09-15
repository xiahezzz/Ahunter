import { X } from "lucide-react";
import { MxEventDetail as EventDetail } from "./contracts";

type Props = {
  detail: EventDetail | null;
  loading: boolean;
  error: string | null;
  failedMedia: ReadonlySet<string>;
  onMediaError: (mediaId: string) => void;
  onClose: () => void;
};

function timestamp(value: string | null): string {
  return value ? value.replace("T", " ").replace(/([+-]\d{2}:\d{2}|Z)$/, "") : "暂无";
}

export default function MxEventDetail({ detail, loading, error, failedMedia, onMediaError, onClose }: Props) {
  return <aside className="mx-event-detail" aria-label="MX 资讯详情">
    <div className="mx-event-detail-heading"><div><p className="eyebrow">MX 资讯详情</p><h2>{detail ? `RID ${detail.rid}` : "正在读取"}</h2></div><button type="button" className="icon-button" aria-label="关闭详情" title="关闭详情" onClick={onClose}><X size={17} /></button></div>
    {loading ? <p className="unavailable-message">正在读取资讯详情…</p> : null}
    {error ? <p className="unavailable-message" role="alert">{error}</p> : null}
    {detail ? <>
      <dl className="mx-detail-metadata">
        <div><dt>授权状态</dt><dd>{detail.authorization === "current" ? "当前 RID" : "历史 RID"}</dd></div>
        <div><dt>来源时间</dt><dd>{timestamp(detail.source_created_at)}</dd></div>
        <div><dt>接收时间</dt><dd>{timestamp(detail.received_at)}</dd></div>
      </dl>
      <div className="mx-detail-blocks">
        {detail.blocks.map((block, index) => block.type === "text" ? <p key={`text-${index}`}>{block.text}</p> : (
          <figure className="mx-media-block" key={block.media_id}>
            {failedMedia.has(block.media_id) ? <div className="mx-media-error" role="status">图片暂不可读取</div> : <a href={block.href} target="_blank" rel="noreferrer" aria-label="在新窗口查看图片"><img src={block.href} alt="MX 资讯图片" onError={() => onMediaError(block.media_id)} /></a>}
            <figcaption>点击图片可放大查看</figcaption>
          </figure>
        ))}
      </div>
    </> : null}
  </aside>;
}
