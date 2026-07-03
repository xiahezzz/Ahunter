import { downloadImage } from "./download-image.mjs";

export async function processEventMedia({
  event,
  store,
  mediaRoot,
  fetchImpl = fetch,
  now = () => Date.now(),
}) {
  const results = [];
  for (const url of event.parsedContent.imageUrls) {
    const media = await downloadImage({ url, mediaRoot, fetchImpl });
    store.insertMedia({
      eventId: event.eventId,
      rid: event.rid,
      downloadedAt: now(),
      ...media,
    });
    results.push(media);
  }
  return results;
}
