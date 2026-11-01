import { Directive, ElementRef, Input, OnDestroy, inject } from '@angular/core';
import { Subscription } from 'rxjs';

import { AuthBlobService, isObjectUrl } from '../core/auth-blob.service';

/**
 * `<img [appAuthSrc]="url">` : comme `[src]`, mais une image servie par une API
 * du lab est lue par HttpClient (jeton dans l'en-tête, jamais dans l'URL) et
 * affichée par une URL `blob:`, révoquée quand l'adresse change ou que
 * l'élément est détruit. Une adresse externe est posée telle quelle.
 */
@Directive({ selector: 'img[appAuthSrc]', standalone: true })
export class AuthSrcDirective implements OnDestroy {
  private el = inject<ElementRef<HTMLImageElement>>(ElementRef);
  private blobs = inject(AuthBlobService);
  private sub?: Subscription;
  private current: string | null = null;
  private requested: string | null = null;

  @Input() set appAuthSrc(url: string | null | undefined) {
    const next = url || null;
    if (next === this.requested) return;
    this.requested = next;
    this.release();
    if (!next) {
      this.el.nativeElement.removeAttribute('src');
      return;
    }
    this.sub = this.blobs.displayUrl(next).subscribe({
      next: href => {
        this.current = href;
        this.el.nativeElement.src = href;
      },
      error: () => this.el.nativeElement.removeAttribute('src'),
    });
  }

  ngOnDestroy(): void {
    this.release();
  }

  private release(): void {
    this.sub?.unsubscribe();
    this.sub = undefined;
    if (isObjectUrl(this.current)) URL.revokeObjectURL(this.current);
    this.current = null;
  }
}
