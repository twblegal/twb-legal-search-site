// Cloudflare Pages Function — handles job application form submissions.
// Receives multipart/form-data, stores the resume in R2, and emails
// the application details (with a link to the resume) using Resend.
//
// SETUP REQUIRED — see SETUP.md in this repo for full step-by-step
// instructions. Summary:
//   1. Create an R2 bucket and bind it to this Pages project as "RESUMES_BUCKET"
//   2. Create a free Resend account (resend.com), verify your sending domain
//   3. Add environment variables in Cloudflare Pages dashboard:
//        RESEND_API_KEY   - your Resend API key
//        NOTIFY_EMAIL     - the email address that should receive applications
//        FROM_EMAIL       - a verified sending address, e.g. apply@twb.legal
//   4. Bind a Worker Route or use the R2 public bucket URL so resume links work
//        R2_PUBLIC_URL    - base public URL for the bucket (see SETUP.md)

export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    const formData = await request.formData();

    const name = (formData.get('name') || '').toString().trim();
    const email = (formData.get('email') || '').toString().trim();
    const phone = (formData.get('phone') || '').toString().trim();
    const position = (formData.get('position') || '').toString().trim();
    const message = (formData.get('message') || '').toString().trim();
    const resumeFile = formData.get('resume');

    // Basic validation
    if (!name || !email || !position) {
      return jsonResponse({ success: false, error: 'Missing required fields.' }, 400);
    }

    if (!resumeFile || typeof resumeFile === 'string') {
      return jsonResponse({ success: false, error: 'Resume file is required.' }, 400);
    }

    const MAX_SIZE = 10 * 1024 * 1024; // 10MB
    if (resumeFile.size > MAX_SIZE) {
      return jsonResponse({ success: false, error: 'Resume file is too large (max 10MB).' }, 400);
    }

    const allowedTypes = [
      'application/pdf',
      'application/msword',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ];
    if (resumeFile.type && !allowedTypes.includes(resumeFile.type)) {
      return jsonResponse({ success: false, error: 'Resume must be a PDF or Word document.' }, 400);
    }

    // Build a unique, safe object key for R2
    const timestamp = Date.now();
    const safeName = name.replace(/[^a-zA-Z0-9]+/g, '_').slice(0, 50);
    const originalExt = (resumeFile.name.split('.').pop() || 'pdf').toLowerCase();
    const objectKey = `resumes/${timestamp}_${safeName}.${originalExt}`;

    // Upload resume to R2
    await env.RESUMES_BUCKET.put(objectKey, resumeFile.stream(), {
      httpMetadata: { contentType: resumeFile.type || 'application/octet-stream' }
    });

    const resumeUrl = `${env.R2_PUBLIC_URL}/${objectKey}`;

    // Compose notification email
    const emailHtml = `
      <h2>New Job Application — TWB Legal Search</h2>
      <p><strong>Position:</strong> ${escapeHtml(position)}</p>
      <p><strong>Name:</strong> ${escapeHtml(name)}</p>
      <p><strong>Email:</strong> ${escapeHtml(email)}</p>
      <p><strong>Phone:</strong> ${escapeHtml(phone || 'Not provided')}</p>
      <p><strong>Note:</strong><br>${escapeHtml(message || 'None').replace(/\n/g, '<br>')}</p>
      <p><strong>Resume:</strong> <a href="${resumeUrl}">${resumeUrl}</a></p>
      <hr>
      <p style="color:#888;font-size:12px;">Submitted ${new Date().toISOString()}</p>
    `;

    const emailResponse = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.RESEND_API_KEY}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from: env.FROM_EMAIL,
        to: env.NOTIFY_EMAIL,
        reply_to: email,
        subject: `New Application: ${position} — ${name}`,
        html: emailHtml
      })
    });

    if (!emailResponse.ok) {
      const errText = await emailResponse.text();
      console.error('Resend error:', errText);
      // Resume is safely stored even if email fails — don't lose the application.
      return jsonResponse({
        success: false,
        error: 'Application was received but the notification email failed. Please also email info@twb.legal to confirm.'
      }, 502);
    }

    return jsonResponse({ success: true });

  } catch (err) {
    console.error('Application submission error:', err);
    return jsonResponse({ success: false, error: 'Unexpected server error.' }, 500);
  }
}

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
