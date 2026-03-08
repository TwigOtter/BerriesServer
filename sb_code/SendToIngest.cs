using System;
using System.Net.Http;
using System.Text;

public class CPHInline
{
    public bool Execute()
    {
        string requestUrl = TryGetArgOrDefault("requestUrl", "");
        string ingestString = TryGetArgOrDefault("ingestString", "");
        if (string.IsNullOrWhiteSpace(requestUrl))
        {
            CPH.LogError("[Berries] SendChatEventToIngest: requestUrl argument is missing.");
            return false;
        }

        if (string.IsNullOrWhiteSpace(ingestString))
        {
            CPH.LogWarn("[Berries] SendChatEventToIngest: ingestString is empty, skipping.");
            return false;
        }

        string ingestSecret = CPH.GetGlobalVar<string>("Berries_IngestSecret", true) ?? "";
        CPH.LogVerbose($"[Berries] POST {requestUrl} — {ingestString}");
        using (var http = new HttpClient())
        {
            http.Timeout = TimeSpan.FromSeconds(10);
            if (!string.IsNullOrWhiteSpace(ingestSecret))
                http.DefaultRequestHeaders.Add("X-Secret", ingestSecret);
            HttpResponseMessage resp;
            try
            {
                var content = new StringContent(ingestString, new UTF8Encoding(false), "application/json");
                resp = http.PostAsync(requestUrl, content).GetAwaiter().GetResult();
            }
            catch (Exception ex)
            {
                CPH.LogError($"[Berries] SendChatEventToIngest: HTTP error — {ex.Message}");
                return false;
            }

            if (!resp.IsSuccessStatusCode)
            {
                string body = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                CPH.LogError($"[Berries] SendChatEventToIngest: {(int)resp.StatusCode} — {body}");
                return false;
            }
        }

        return true;
    }

    private T TryGetArgOrDefault<T>(string argName, T defaultValue)
    {
        if (CPH.TryGetArg(argName, out T value))
            return value;
        CPH.LogDebug($"[Berries] SendChatEventToIngest: arg '{argName}' not found, using default '{defaultValue}'");
        return defaultValue;
    }
}
