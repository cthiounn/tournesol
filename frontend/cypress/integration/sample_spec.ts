const baseUrl = Cypress.config('baseUrl');

describe('My First Test', () => {
  it('Does not do much!', () => {
    expect(true).to.equal(true)
  })

  it('Checks home page', () => {
    cy.visit('/');
    cy.contains('Tournesol').should('exist');
    cy.contains('Log in').click();
    cy.url().should('include', '/login')
    cy.focused().type('username');
    cy.get('input[name="password"]').click().type('password').type('{enter}');
    cy.url().should('equal', `${baseUrl}/`);
    cy.contains('Logout').should('exist');
  })
})
